"""x402 (USDC on Base) metering middleware with real facilitator verification.

Per-call micropayments are verified against an x402 facilitator (EIP-3009
authorization, ``"exact"`` scheme, USDC on Base mainnet). Flow per paid request:

    X-Payment header (base64 JSON v1 payload) -> decode -> local sanity checks
    -> anti-replay -> POST {facilitator}/verify -> serve -> POST {facilitator}/settle

Payments are OFF by default: the middleware is a no-op unless
``x402_effective_enabled()`` (``X402_ENABLED=true`` AND ``X402_PAY_TO`` AND
``X402_FACILITATOR_URL`` all set). If the flag is on but config is incomplete,
the middleware logs an ERROR at startup and stays disabled (fail closed).
Network failure talking to the facilitator -> 503, never a free serve.

IMPORTANT: the default ``X402_FACILITATOR_URL`` (``https://x402.org/facilitator``)
is **Base Sepolia testnet only — NOT mainnet**. For real USDC on Base mainnet,
set ``X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402`` with
``CDP_API_KEY_ID``/``CDP_API_KEY_SECRET``. The exact CDP claim set in the minted
JWT needs a live smoke test with Scott's keys before mainnet enablement.

Stripe subscriber keys (see ``billing.py``) bypass per-call charges when valid.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import billing

log = logging.getLogger("price_tracker.x402")

# Circle's canonical USDC contract on Base (6 decimals).
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA38bD"
USDC_DECIMALS = 6
SCHEME = "exact"
NETWORK = "base"
X402_VERSION = 1
MAX_TIMEOUT_SECONDS = 300
FACILITATOR_TIMEOUT_SECONDS = 15.0

# Default facilitator: Base Sepolia testnet ONLY. See module docstring.
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"

# Paths that never require payment (health checks, docs, legal pages).
# Prefix matching covers subpaths, e.g. "/legal" covers /legal/terms.
EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico", "/legal")

# JSON-RPC methods that stay free (discovery must work for directory scans).
JSONRPC_DISCOVERY_METHODS = {"initialize", "tools/list", "ping"}

# REST path -> tool name for per-tool pricing.
REST_TOOL_MAP = {
    "/price": "check_price",
    "/affiliate": "make_affiliate_link",
}

# Proposed per-tool price defaults in USDC. Pricing is a business decision —
# these are baked-in defaults, overridable per tool via X402_PRICE_<NAME>_USDC.
# Flagged for Scott's call in the PR.
TOOL_PRICE_DEFAULTS = {
    "check_price": Decimal("0.02"),
    "make_affiliate_link": Decimal("0.005"),
}

TOOL_DESCRIPTIONS = {
    "check_price": "check_price: check the current price of a product page",
    "make_affiliate_link": "make_affiliate_link: build an affiliate tracking link",
}

DEFAULT_PRICE_USDC = Decimal("0.015")


# --------------------------------------------------------------------------
# Configuration


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def x402_enabled() -> bool:
    """Raw X402_ENABLED flag (not the effective state)."""
    return _env_flag("X402_ENABLED")


def x402_pay_to() -> str:
    return os.environ.get("X402_PAY_TO", "").strip()


def x402_facilitator_url() -> str:
    return os.environ.get("X402_FACILITATOR_URL", DEFAULT_FACILITATOR_URL).strip()


def x402_effective_enabled() -> bool:
    """True only when the flag is on AND recipient AND facilitator URL are set.

    Flag on but config incomplete -> treated as disabled (fail closed).
    """
    return x402_enabled() and bool(x402_pay_to()) and bool(x402_facilitator_url())


def _parse_price(raw: str | None, name: str, default: Decimal) -> Decimal:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        log.warning("Invalid %s=%r; falling back to %s (never zero)", name, raw, default)
        return default
    if value < 0:
        log.warning("Negative %s=%r; falling back to %s (never zero)", name, raw, default)
        return default
    return value


def _tool_env_name(tool: str) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in tool.upper())
    return f"X402_PRICE_{cleaned}_USDC"


def tool_price_usdc(tool: str) -> Decimal:
    """Resolved USDC price for a tool: env override -> baked-in default."""
    default = TOOL_PRICE_DEFAULTS.get(tool, DEFAULT_PRICE_USDC)
    return _parse_price(os.environ.get(_tool_env_name(tool)), _tool_env_name(tool), default)


def default_price_usdc() -> Decimal:
    return _parse_price(os.environ.get("X402_PRICE_USDC"), "X402_PRICE_USDC", DEFAULT_PRICE_USDC)


def usdc_to_atomic(price: Decimal) -> int:
    """USDC decimal -> atomic units (6 decimals). Exact for Decimal input."""
    return int(price * (10**USDC_DECIMALS))


# Fair-use credit unit: 1 credit = $0.01 of x402 list price. Credit cost per
# call = round(price_usd / 0.01, 1): $0.025 -> 2.5, $0.01 -> 1.0, $0.005 -> 0.5.
CREDIT_USD = Decimal("0.01")


def price_to_credits(price: Decimal) -> float:
    """x402 list price -> fair-use credit cost for one call.

    Single source of truth: the price comes from ``tool_price_usdc`` (the
    same per-tool price map the x402 middleware charges from) — this function
    only converts it. Env price overrides (``X402_PRICE_<NAME>_USDC``) flow
    through automatically.
    """
    return float(round(price / CREDIT_USD, 1))


# --------------------------------------------------------------------------
# paymentRequirements (built server-side from env, never client-asserted)


def build_payment_requirements(
    price: Decimal, resource: str, description: str, pay_to: str
) -> dict:
    return {
        "scheme": SCHEME,
        "network": NETWORK,
        "maxAmountRequired": str(usdc_to_atomic(price)),
        "resource": resource,
        "description": description,
        "payTo": pay_to or "<set X402_PAY_TO>",
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "asset": USDC_BASE_ADDRESS,
    }


# --------------------------------------------------------------------------
# X-Payment decoding + local sanity checks


def decode_x_payment(raw: str) -> dict | None:
    """Decode the X-Payment header: base64 -> JSON v1 payment payload."""
    try:
        padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
        payload = json.loads(base64.b64decode(padded, validate=True))
    except (ValueError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def sanity_check_failure(payment: dict, requirements: dict) -> str | None:
    """Local pre-checks before calling the facilitator.

    Returns an invalidReason string when the payload fails, else None.
    Fail closed: any malformed field -> failure.
    """
    try:
        if payment.get("x402Version") != X402_VERSION:
            return f"unsupported x402Version (want {X402_VERSION})"
        if payment.get("scheme") != SCHEME:
            return f'unsupported scheme (want "{SCHEME}")'
        if payment.get("network") != NETWORK:
            return f'unsupported network (want "{NETWORK}")'
        inner = payment.get("payload")
        if not isinstance(inner, dict):
            return "missing payload object"
        auth = inner.get("authorization")
        if not isinstance(auth, dict):
            return "missing payload.authorization object"
        pay_to = str(requirements.get("payTo", ""))
        if not pay_to or str(auth.get("to", "")).lower() != pay_to.lower():
            return "authorization 'to' does not match payTo"
        value = int(str(auth.get("value", "0")))
        required = int(str(requirements.get("maxAmountRequired", "0")))
        if value < required:
            return f"insufficient value: {value} < {required} atomic units"
        now = int(time.time())
        valid_after = int(str(auth.get("validAfter", "0")))
        valid_before = int(str(auth.get("validBefore", "0")))
        if not (valid_after <= now < valid_before):
            return "authorization outside validAfter/validBefore window"
        if not str(inner.get("signature", "")).startswith("0x"):
            return "missing signature"
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return f"malformed payment payload: {exc}"
    return None


# --------------------------------------------------------------------------
# Anti-replay (in-memory, TTL = maxTimeoutSeconds)


_seen_signatures: dict[str, float] = {}
_seen_lock = threading.Lock()


def check_replay(signature: str) -> bool:
    """True if this signature was seen within the TTL (duplicate payment)."""
    now = time.time()
    with _seen_lock:
        expired = [s for s, ts in _seen_signatures.items() if now - ts > MAX_TIMEOUT_SECONDS]
        for s in expired:
            del _seen_signatures[s]
        if signature in _seen_signatures:
            return True
        _seen_signatures[signature] = now
        return False


def _reset_replay_cache() -> None:  # for tests
    with _seen_lock:
        _seen_signatures.clear()


# --------------------------------------------------------------------------
# CDP JWT minting (Coinbase CDP facilitator auth)


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_ed25519_key(secret: str) -> ed25519.Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM, base64, or hex.

    Accepts a PEM-encoded PKCS#8 private-key block, or the raw key
    material (32-byte seed or 64-byte private key) encoded as base64
    (standard or urlsafe) or hex.
    """
    s = secret.strip()
    if "BEGIN" in s:
        key = serialization.load_pem_private_key(s.encode("utf-8"), password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ValueError("PEM key is not an Ed25519 private key")
        return key
    # Raw key material: 32-byte seed or 64-byte private key, as base64
    # (standard or urlsafe) or hex. Length-checked: a string that decodes to
    # the wrong length under one encoding is retried under the others.
    raw: bytes | None = None
    decoders = (
        lambda v: base64.b64decode(v + "=" * (-len(v) % 4), validate=True),
        lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)),
    )
    for decoder in decoders:
        try:
            candidate = decoder(s)
        except (ValueError, binascii.Error):
            continue
        if len(candidate) in (32, 64):
            raw = candidate
            break
    if raw is None:
        try:
            candidate = bytes.fromhex(s)
        except ValueError:
            candidate = b""
        if len(candidate) in (32, 64):
            raw = candidate
    if raw is None:
        raise ValueError(
            "CDP secret must be a PEM block or base64/hex Ed25519 key material "
            "(32-byte seed or 64-byte private key)"
        )
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])


def mint_cdp_jwt(key_id: str, key_secret: str, now: int | None = None) -> str:
    """Mint a short-lived EdDSA JWT for the Coinbase CDP facilitator API.

    Header: ``{"alg": "EdDSA", "typ": "JWT", "kid": key_id, "nonce": <hex>}``.
    Claims: ``{"sub": key_id, "iss": "coinbase-cloud", "nbf": now-30,
    "exp": now+120}``.

    NOTE: the exact CDP claim set needs a live smoke test with Scott's keys
    before mainnet enablement.
    """
    key = _load_ed25519_key(key_secret)
    ts = now if now is not None else int(time.time())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": key_id, "nonce": secrets.token_hex(16)}
    claims = {"sub": key_id, "iss": "coinbase-cloud", "nbf": ts - 30, "exp": ts + 120}
    signing_input = (
        _b64url_nopad(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_nopad(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = key.sign(signing_input)
    return signing_input.decode("ascii") + "." + _b64url_nopad(signature)


def _facilitator_headers() -> dict:
    """Auth headers for the facilitator, per env config (pluggable)."""
    api_key = os.environ.get("X402_FACILITATOR_API_KEY", "").strip()
    if api_key:
        return {"X-API-Key": api_key}
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    if key_id and key_secret:
        return {"Authorization": f"Bearer {mint_cdp_jwt(key_id, key_secret)}"}
    return {}


# --------------------------------------------------------------------------
# Facilitator client (x402 v1 "exact" scheme)


class FacilitatorUnavailable(Exception):
    """The facilitator could not be reached or answered badly. Fail closed."""


def _facilitator_base() -> str:
    return x402_facilitator_url().rstrip("/")


async def facilitator_verify(
    payment_payload: dict, requirements: dict
) -> tuple[bool, str | None, str | None]:
    """POST {facilitator}/verify. Returns (is_valid, invalid_reason, payer)."""
    body = {
        "x402Version": X402_VERSION,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_facilitator_base()}/verify", json=body, headers=_facilitator_headers()
            )
    except (httpx.HTTPError, OSError) as exc:
        raise FacilitatorUnavailable(str(exc)) from exc
    if resp.status_code >= 500:
        raise FacilitatorUnavailable(f"facilitator HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise FacilitatorUnavailable("facilitator returned non-JSON") from exc
    if not isinstance(data, dict):
        raise FacilitatorUnavailable("facilitator returned non-object JSON")
    return (
        bool(data.get("isValid")),
        data.get("invalidReason"),
        data.get("payer"),
    )


async def facilitator_settle(payment_payload: dict, requirements: dict) -> dict:
    """POST {facilitator}/settle. Returns the raw response dict (normalized)."""
    body = {
        "x402Version": X402_VERSION,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{_facilitator_base()}/settle", json=body, headers=_facilitator_headers()
            )
    except (httpx.HTTPError, OSError) as exc:
        raise FacilitatorUnavailable(str(exc)) from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise FacilitatorUnavailable("facilitator returned non-JSON") from exc
    if not isinstance(data, dict):
        data = {}
    # Accept any of the tx-hash field spellings the facilitator may use.
    tx_hash = (
        data.get("transaction")
        or data.get("txHash")
        or data.get("transactionHash")
        or data.get("hash")
    )
    return {
        "success": bool(data.get("success")),
        "transaction": tx_hash,
        "network": data.get("network"),
        "payer": data.get("payer"),
        "errorReason": data.get("errorReason"),
    }


# --------------------------------------------------------------------------
# 402 responses


def _challenge_content(
    error: str, message: str, requirements: dict, price: Decimal, pay_to: str
) -> dict:
    return {
        "error": error,
        "message": message,
        "accepts": {
            "asset": "USDC",
            "network": NETWORK,
            "amount": str(price),
            "pay_to": pay_to or "<set X402_PAY_TO>",
            "scheme": SCHEME,
        },
        "paymentRequirements": requirements,
    }


def payment_required_response(requirements: dict, price: Decimal, pay_to: str) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content=_challenge_content(
            "payment_required",
            (
                "This endpoint requires an x402 USDC micropayment on Base. "
                "Send an X-Payment header with a base64-encoded signed "
                "EIP-3009 authorization."
            ),
            requirements,
            price,
            pay_to,
        ),
    )


def payment_invalid_response(
    requirements: dict, price: Decimal, pay_to: str, code: str, detail: str
) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content=_challenge_content(
            code,
            f"Payment rejected: {detail}",
            requirements,
            price,
            pay_to,
        ),
    )


def subscriber_limit_response(usage: dict) -> JSONResponse:
    """HTTP 429 for a subscriber key that exceeded its monthly fair-use cap.

    Never silently serves: the body carries the credit cap, the current
    credit usage, the reset date (first of next month, UTC), and the x402
    per-call fallback so the subscriber can keep working right now.
    """
    return JSONResponse(
        status_code=429,
        content={
            "error": "subscriber_monthly_cap_exceeded",
            "message": (
                f"Monthly fair-use credit cap reached: {usage['credits_used']} of "
                f"{usage['credit_limit']} credits used. Your allowance resets "
                f"on {usage['reset_at']}. You can keep using this endpoint "
                "right now by paying per call with x402 — resend the request "
                "without X-Subscriber-Key and include a signed X-Payment header."
            ),
            "credits_used": usage["credits_used"],
            "credit_limit": usage["credit_limit"],
            "resets_at": usage["reset_at"],
        },
    )


# --------------------------------------------------------------------------
# Price resolution


def _default_price(description: str) -> tuple[Decimal, str]:
    return default_price_usdc(), description


async def _resolve_price(request: Request) -> tuple[Decimal, str] | None:
    """Resolve the USDC price + description for this request.

    Returns None for paths that are exempt from payment (discovery).
    Unparseable bodies fall back to the default price, never a 500.
    """
    path = request.url.path
    if path.startswith("/mcp") and request.method == "POST":
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return _default_price("mcp request")
        if not isinstance(body, dict):
            return _default_price("mcp request")
        method = body.get("method")
        if method in JSONRPC_DISCOVERY_METHODS or (
            isinstance(method, str) and method.startswith("notifications/")
        ):
            return None  # discovery stays free
        if method == "tools/call":
            params = body.get("params")
            tool = params.get("name") if isinstance(params, dict) else None
            if isinstance(tool, str) and tool in TOOL_PRICE_DEFAULTS:
                return tool_price_usdc(tool), TOOL_DESCRIPTIONS[tool]
            return _default_price(f"mcp tools/call {tool!r}")
        return _default_price(f"mcp {method}")
    tool = REST_TOOL_MAP.get(path)
    if tool is not None:
        return tool_price_usdc(tool), TOOL_DESCRIPTIONS[tool]
    if path.startswith("/billing"):
        # Billing flow has its own enablement logic; never x402-gated.
        return None
    return _default_price(path)


# --------------------------------------------------------------------------
# Middleware


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Require a facilitator-verified X-Payment header on paid paths."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if path.startswith("/mcp") and request.method == "GET":
            # Allow MCP discovery/info GETs; tool calls are POST.
            return await call_next(request)
        if not x402_effective_enabled():
            return await call_next(request)

        # Stripe subscriber bypass: valid key -> no per-call charge, but the
        # monthly fair-use CREDIT cap still applies (see
        # billing.record_subscriber_usage). The cost of this call is derived
        # from the same per-tool price map the x402 flow charges from — no
        # duplicated price table. The cap covers every subscriber-key-
        # authenticated call (REST and MCP) because all of them pass through
        # this middleware. Invalid/expired key falls through to the normal
        # x402 flow (not an error). x402 per-call payers are never capped.
        sub_key = request.headers.get("x-subscriber-key")
        if sub_key and billing.valid_subscriber_key(sub_key):
            if path == "/billing/usage":
                # Read-only usage peek: never deducts, never 429-gated.
                return await call_next(request)
            resolved = await _resolve_price(request)
            cost = price_to_credits(resolved[0]) if resolved is not None else 0.0
            usage = billing.record_subscriber_usage(sub_key, cost)
            if not usage["allowed"]:
                return subscriber_limit_response(usage)
            return await call_next(request)

        resolved = await _resolve_price(request)
        if resolved is None:
            return await call_next(request)
        price, description = resolved
        pay_to = x402_pay_to()
        requirements = build_payment_requirements(price, str(request.url), description, pay_to)

        raw = request.headers.get("x-payment")
        if not raw:
            return payment_required_response(requirements, price, pay_to)
        payment = decode_x_payment(raw)
        if payment is None:
            return payment_required_response(requirements, price, pay_to)
        failure = sanity_check_failure(payment, requirements)
        if failure is not None:
            return payment_invalid_response(requirements, price, pay_to, "payment_invalid", failure)
        signature = str(payment["payload"]["signature"])
        if check_replay(signature):
            return payment_invalid_response(
                requirements,
                price,
                pay_to,
                "duplicate_payment",
                "this payment signature was already used",
            )
        try:
            is_valid, invalid_reason, payer = await facilitator_verify(payment, requirements)
        except FacilitatorUnavailable:
            # Fail closed: never serve paid content without verification.
            return JSONResponse(
                status_code=503,
                content={
                    "error": "payment_verification_unavailable",
                    "message": (
                        "The x402 facilitator could not be reached; "
                        "payment cannot be verified right now. Please retry."
                    ),
                },
            )
        if not is_valid:
            return payment_invalid_response(
                requirements,
                price,
                pay_to,
                "payment_invalid",
                invalid_reason or "facilitator rejected the payment",
            )

        response = await call_next(request)

        # Settle after serving. A settle failure is logged loudly (money may be
        # lost) but never fails the already-served request.
        atomic = requirements["maxAmountRequired"]
        try:
            result = await facilitator_settle(payment, requirements)
        except FacilitatorUnavailable as exc:
            log.error(
                "x402 SETTLE FAILED (facilitator unreachable): %s — "
                "payer=%s amount_atomic=%s — payment may be unclaimed",
                exc,
                payer,
                atomic,
            )
        else:
            if result["success"]:
                log.info(
                    "x402 settled: tx=%s payer=%s amount_atomic=%s",
                    result["transaction"],
                    result["payer"] or payer,
                    atomic,
                )
            else:
                log.error(
                    "x402 SETTLE FAILED: error=%s payer=%s amount_atomic=%s — "
                    "payment may be unclaimed",
                    result["errorReason"],
                    result["payer"] or payer,
                    atomic,
                )
        return response


# --------------------------------------------------------------------------
# Startup


def warn_if_payments_live() -> None:
    if x402_enabled() and not x402_effective_enabled():
        log.error(
            "X402_ENABLED=true but X402_PAY_TO / X402_FACILITATOR_URL incomplete — "
            "x402 DISABLED (fail closed). Set the missing config or flip the flag off."
        )
    elif x402_effective_enabled():
        log.warning(
            "X402 payments LIVE — real USDC charges on Base. "
            "Confirm AZ LLC is formed before the first dollar arrives."
        )
    if billing.stripe_flag_on() and not billing.stripe_effective_enabled():
        log.error(
            "STRIPE_ENABLED=true but STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / "
            "STRIPE_PRICE_ID incomplete — Stripe DISABLED (fail closed)."
        )
    elif billing.stripe_effective_enabled():
        log.warning("Stripe billing LIVE — checkout and webhook endpoints active.")


# Backwards compatibility for the old stub API (kept for tests/imports).
def verify_payment_stub(payment: dict, price: Decimal, pay_to: str) -> bool:  # noqa: ANN001, ANN202
    """Deprecated stub — replaced by facilitator-backed verification."""
    log.warning("verify_payment_stub is deprecated; use facilitator_verify instead")
    return False


def warn_if_stub_enabled() -> None:  # noqa: ANN202
    """Deprecated — replaced by warn_if_payments_live."""
    warn_if_payments_live()


# Re-export for tests without importing the private name.
__all__ = [
    "X402PaymentMiddleware",
    "warn_if_payments_live",
    "x402_effective_enabled",
    "x402_enabled",
    "x402_pay_to",
    "x402_facilitator_url",
    "tool_price_usdc",
    "default_price_usdc",
    "usdc_to_atomic",
    "build_payment_requirements",
    "decode_x_payment",
    "sanity_check_failure",
    "check_replay",
    "mint_cdp_jwt",
    "facilitator_verify",
    "facilitator_settle",
    "FacilitatorUnavailable",
    "payment_required_response",
    "payment_invalid_response",
    "subscriber_limit_response",
    "price_to_credits",
    "USDC_BASE_ADDRESS",
    "TOOL_PRICE_DEFAULTS",
]
