"""x402 (USDC on Base; opt-in Solana leg) metering middleware with real facilitator verification.

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

Free tier: when x402 is on, anonymous callers (no subscriber key, no
X-Payment) draw from a monthly free credit bucket keyed by client IP
(``FREE_MONTHLY_CREDITS``, default 50 — Scott's call; kill switch
``FREE_TIER_ENABLED``). Same credit math as subscribers (``price_to_credits``
on the single per-tool price map). Exhaustion -> HTTP 402
``free_tier_exhausted`` — 429 stays exclusive to the subscriber cap. Abuse
backstop is the existing 60/min per-IP rate limit; IP rotation is an
accepted v1 limitation (see PR body).

Every served call on a metered path is rolled into the ``usage_daily``
analytics aggregates (see ``billing.record_usage_rollup``): date, tool,
plan_type (free|subscriber|x402) — no PII, no IPs.
"""

from __future__ import annotations

import base64
import binascii
import copy
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
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
USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
SCHEME = "exact"
NETWORK = "base"
X402_VERSION = 1
MAX_TIMEOUT_SECONDS = 300
FACILITATOR_TIMEOUT_SECONDS = 15.0

# Default facilitator: Base Sepolia testnet ONLY. See module docstring.
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"

# Solana leg (opt-in via X402_NETWORKS). SOLANA_NETWORK is the x402 v1
# plain network name (SOLANA_PORT_PLAN §2); the CAIP-2 form
# (solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp) is a v2 identifier. Plan open
# question 1 — confirm the exact string against CDP /supported — stays
# open until the devnet round-trip (§4d); "solana" is the plan default.
SOLANA_NETWORK = "solana"
# Circle's canonical USDC SPL mint on Solana mainnet (6 decimals).
USDC_SOLANA_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
KNOWN_NETWORKS = (NETWORK, SOLANA_NETWORK)

# Paths that never require payment (health checks, docs, legal pages).
# Prefix matching covers subpaths, e.g. "/legal" covers /legal/terms, and
# "/.well-known" covers the x402 discovery manifest (/.well-known/x402.json),
# which must stay reachable for directory scans even when x402 is enabled.
EXEMPT_PREFIXES = (
    # Landing page: the matcher is exact-or-prefix+"/", so "/" exempts ONLY
    # the bare root path — it does not widen to any other route.
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/legal",
    "/support",
    "/demo-walkthrough.mp4",
    "/.well-known",
)

# JSON-RPC methods that stay free (discovery must work for directory scans).
JSONRPC_DISCOVERY_METHODS = {
    "initialize",
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "ping",
}

# Always-paid route prefix (2026-10-24 paid-route batch): POSTs under this
# prefix NEVER draw from the monthly free-credit bucket — without a valid
# X-Payment they fall through to the 402 challenge. Directory checkers
# (PayAPI preflight) and router dispatch (Agent402) must see a 402 to
# price a route, and the free tier answers them 200 instead. Normal
# routes keep the free tier unchanged; only /paid/* aliases skip it.
PAID_ROUTE_PREFIX = "/paid/"

# REST path -> tool name for per-tool pricing.
REST_TOOL_MAP = {
    "/price": "check_price",
    "/affiliate": "make_affiliate_link",
    "/best-price": "find_best_price",
    # Always-paid alias: same tool as /price, never free-tiered.
    "/paid/price": "check_price",
    "/paid/best-price": "find_best_price",
}

# Proposed per-tool price defaults in USDC. Pricing is a business decision —
# these are baked-in defaults, overridable per tool via X402_PRICE_<NAME>_USDC.
# Flagged for Scott's call in the PR.
TOOL_PRICE_DEFAULTS = {
    "check_price": Decimal("0.008"),
    "make_affiliate_link": Decimal("0.005"),
    # v2 multi-source comparison: $0.03/call per the tapped spec
    # (price-tracker-v2-spec.md section 4, Scott's call 2026-10-31).
    "find_best_price": Decimal("0.03"),
}

TOOL_DESCRIPTIONS = {
    "check_price": "check_price: check the current price of a product page",
    "make_affiliate_link": "make_affiliate_link: build an affiliate tracking link",
    "find_best_price": "find_best_price: compare prices across reputable stores",
}

DEFAULT_PRICE_USDC = Decimal("0.008")


# --------------------------------------------------------------------------
# Configuration


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def x402_enabled() -> bool:
    """Raw X402_ENABLED flag (not the effective state)."""
    return _env_flag("X402_ENABLED")


def x402_pay_to() -> str:
    return os.environ.get("X402_PAY_TO", "").strip()


def x402_pay_to_solana() -> str:
    return os.environ.get("X402_PAY_TO_SOLANA", "").strip()


def x402_facilitator_url() -> str:
    return os.environ.get("X402_FACILITATOR_URL", DEFAULT_FACILITATOR_URL).strip()


def solana_facilitator_url() -> str:
    """Facilitator for the Solana leg: dedicated override, else the shared one."""
    return os.environ.get("X402_FACILITATOR_URL_SOLANA", "").strip() or x402_facilitator_url()


def x402_asset_solana() -> str:
    return os.environ.get("X402_ASSET_SOLANA", USDC_SOLANA_MINT).strip() or USDC_SOLANA_MINT


def requested_networks() -> list[str]:
    """Networks named by X402_NETWORKS. Unset = ["base"] (today's behavior)."""
    raw = os.environ.get("X402_NETWORKS", "").strip()
    if not raw:
        return [NETWORK]
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return names or [NETWORK]


# Cached facilitator Solana fee payer, filled lazily by
# resolve_solana_fee_payer() from GET {facilitator}/supported (plan §3b).
_solana_fee_payer_cache: str | None = None


def solana_fee_payer() -> str | None:
    """The facilitator's Solana fee-payer key: X402_SOLANA_FEE_PAYER env
    override, else the /supported-discovered value. None = unresolved."""
    override = os.environ.get("X402_SOLANA_FEE_PAYER", "").strip()
    if override:
        return override
    return _solana_fee_payer_cache


def x402_effective_enabled() -> bool:
    """True only when the flag is on AND the config is VALID.

    Flag on but config incomplete/invalid -> treated as disabled (fail
    closed), with the specific problems logged.
    """
    if not x402_enabled():
        return False
    problems = x402_config_problems()
    if problems:
        for problem in problems:
            log.error(
                "x402 misconfigured: %s — x402 DISABLED (fail closed).",
                problem,
            )
        return False
    return True


_PAY_TO_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _https_url_problem(label: str, url: str) -> str | None:
    if not url:
        return f"{label} is empty"
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        return (
            f"{label}={url!r} must be an https URL "
            "(http would leak facilitator credentials in cleartext)"
        )
    return None


def leg_config_problems(network: str) -> list[str]:
    """Config problems for ONE network leg. Empty list = leg is servable."""
    problems: list[str] = []
    if network == NETWORK:
        pay_to = x402_pay_to()
        if not pay_to:
            problems.append("X402_PAY_TO is empty")
        elif not _PAY_TO_RE.match(pay_to):
            problems.append(f"X402_PAY_TO={pay_to!r} is not a valid 0x Ethereum address")
        problem = _https_url_problem("X402_FACILITATOR_URL", x402_facilitator_url())
        if problem:
            problems.append(problem)
    elif network == SOLANA_NETWORK:
        pay_to = x402_pay_to_solana()
        if not pay_to:
            problems.append("X402_PAY_TO_SOLANA is empty")
        elif not _BASE58_RE.match(pay_to):
            problems.append(f"X402_PAY_TO_SOLANA={pay_to!r} is not a valid base58 Solana address")
        override = os.environ.get("X402_FACILITATOR_URL_SOLANA", "").strip()
        if override:
            problem = _https_url_problem("X402_FACILITATOR_URL_SOLANA", override)
            if problem:
                problems.append(problem)
    else:
        problems.append(f"unknown x402 network {network!r} in X402_NETWORKS")
    return problems


def valid_networks() -> list[str]:
    """Requested networks whose leg config is complete.

    Per-leg fail-closed (plan §4b.5): a leg with incomplete config is
    dropped with an ERROR log; the remaining legs keep working.
    """
    valid: list[str] = []
    for name in requested_networks():
        problems = leg_config_problems(name)
        if problems:
            for problem in problems:
                log.error("x402 %s leg dropped: %s", name, problem)
        else:
            valid.append(name)
    return valid


def x402_config_problems() -> list[str]:
    """Fail-closed config validation (M-5). Empty list = safe to enable.

    A typo'd X402_PAY_TO would otherwise verify-and-settle real USDC to a
    dead address (service delivered, revenue burned, no error ever raised),
    and an http:// facilitator URL would send facilitator credentials in
    cleartext. Validation is per-leg: one bad leg disables x402 entirely
    only when NO valid leg remains (in which case every leg's problems are
    surfaced so the operator sees exactly what to fix).
    """
    problems: list[str] = []
    if not x402_enabled():
        return problems  # disabled by flag: nothing to validate
    if not valid_networks():
        for name in requested_networks():
            problems.extend(leg_config_problems(name))
        if not problems:
            problems.append("no x402 network legs configured")
    return problems


def network_configs() -> dict[str, dict]:
    """Per-network config table for every currently servable leg (§4b.1)."""
    configs: dict[str, dict] = {}
    for name in valid_networks():
        if name == NETWORK:
            configs[name] = {
                "network_id": NETWORK,
                "asset": USDC_BASE_ADDRESS,
                "pay_to": x402_pay_to(),
                "decimals": USDC_DECIMALS,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        elif name == SOLANA_NETWORK:
            fee_payer = solana_fee_payer()
            if not fee_payer:
                # §4b.5: without a fee payer the client cannot build the
                # transaction — the leg is unservable, drop it loudly.
                log.error(
                    "x402 solana leg dropped: fee payer unresolved (set "
                    "X402_SOLANA_FEE_PAYER or make the facilitator "
                    "/supported endpoint reachable)"
                )
                continue
            configs[name] = {
                "network_id": SOLANA_NETWORK,
                "asset": x402_asset_solana(),
                "pay_to": x402_pay_to_solana(),
                "decimals": USDC_DECIMALS,
                "extra": {"feePayer": fee_payer},
            }
    return configs


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
# Discovery manifest (/.well-known/x402.json)


def build_discovery_manifest() -> dict:
    """Public x402 discovery manifest.

    HONESTY CONTRACT: x402 is implemented but DISABLED until the business
    entity / Stripe go-live, so this manifest NEVER claims payments are live.
    ``x402_status`` is ``"implemented_but_disabled"`` whenever
    ``x402_effective_enabled()`` is False; it only reads otherwise once the
    flag + recipient + facilitator are all configured and charges can
    actually be verified. Every price is derived from ``TOOL_PRICE_DEFAULTS``
    via ``tool_price_usdc()`` — there is deliberately NO duplicated price
    table here. ``pay_to`` is the live ``X402_PAY_TO`` value or None (never
    an invented address).
    """
    live = x402_effective_enabled()
    tools: dict[str, dict] = {}
    for tool in TOOL_PRICE_DEFAULTS:
        rest_path = next((p for p, t in REST_TOOL_MAP.items() if t == tool), None)
        tools[tool] = {
            "price_usdc": str(tool_price_usdc(tool)),
            "rest_path": rest_path,
            "description": TOOL_DESCRIPTIONS.get(tool, tool),
        }
    networks: list[dict] = []
    for name in requested_networks():
        if name == NETWORK:
            networks.append(
                {
                    "network": NETWORK,
                    "asset": "USDC",
                    "asset_contract": USDC_BASE_ADDRESS,
                    "asset_decimals": USDC_DECIMALS,
                    "pay_to": x402_pay_to() or None,
                }
            )
        elif name == SOLANA_NETWORK:
            networks.append(
                {
                    "network": SOLANA_NETWORK,
                    "asset": "USDC",
                    "asset_contract": x402_asset_solana(),
                    "asset_decimals": USDC_DECIMALS,
                    "pay_to": x402_pay_to_solana() or None,
                }
            )
    return {
        "x402_status": "accepting_payments" if live else "implemented_but_disabled",
        "status_note": (
            "x402 payments are enabled: per-call USDC micropayments on Base "
            "are verified against the configured facilitator."
            if live
            else "x402 payments go live after business entity setup; currently disabled."
        ),
        "service": "price-tracker-mcp",
        "version": X402_VERSION,
        "payment": {
            "scheme": SCHEME,
            "network": NETWORK,
            "asset": "USDC",
            "asset_address": USDC_BASE_ADDRESS,
            "asset_decimals": USDC_DECIMALS,
            "pay_to": x402_pay_to() or None,
        },
        "networks": networks,
        "tools": tools,
        "legal": {
            "terms": "/legal/terms",
            "privacy": "/legal/privacy",
            "refunds": "/legal/refunds",
        },
    }


# --------------------------------------------------------------------------
# Bazaar discovery metadata (x402 v1 outputSchema + v2 extensions.bazaar).
# --------------------------------------------------------------------------
# Two cataloging surfaces fed by the single set of tables below:
#
# * x402 v1 (the protocol these servers speak): the facilitator's bazaar
#   extractor reads discovery metadata from ``outputSchema`` INSIDE each
#   paymentRequirements object — ``input`` must carry ``type: "http"`` and
#   the HTTP ``method``; for body endpoints the example request body rides
#   in ``bodyFields`` (reference: x402 Python SDK, x402/extensions/bazaar —
#   "V1 Support" — and the v1 facilitator extractor contract).
# * x402 v2: the same metadata as a top-level ``extensions.bazaar`` block on
#   the 402 body, emitted in the facilitator-facing form the SDK's
#   ``declare_discovery_extension`` / ``declare_mcp_discovery_extension``
#   produce after resource-server enrichment (``info.input`` carries the
#   real method and validates against the sibling JSON Schema; reference:
#   the same SDK's resource_service.py / types.py, mirrored field-for-field
#   below).
#
# Purely additive: existing x402 clients ignore both keys, and no existing
# field changes value.

_BAZAAR_SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"

_PRICE_INFO_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": "number"},
        "currency": {"type": "string"},
        "title": {"type": ["string", "null"]},
        "availability": {"type": ["string", "null"]},
        "source": {"type": "string"},
    },
    "required": ["price", "currency", "source"],
}

_PRICE_INFO_EXAMPLE = {
    "price": 129.99,
    "currency": "USD",
    "title": "Example product",
    "availability": "InStock",
    "source": "json-ld",
}

_BEST_PRICE_SCHEMA = {
    "type": "object",
    "properties": {
        "identity": {"type": "object"},
        "condition": {"type": "string"},
        "country": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["best_offer", "best_found_total_unverified", "no_confident_match"],
        },
        "best_offer": {"type": ["object", "null"]},
        "best_found": {"type": ["object", "null"]},
        "offers": {"type": "array", "items": {"type": "object"}},
        "source_status": {"type": "object"},
        "fetched_at": {"type": "string"},
    },
    "required": ["identity", "verdict", "offers", "source_status"],
}

_BEST_PRICE_EXAMPLE = {
    "identity": {
        "kind": "upc",
        "upc": "887276925174",
        "brand": "samsung",
        "mpn": None,
        "title": None,
        "source_url": None,
    },
    "condition": "new",
    "country": "US",
    "verdict": "best_found_total_unverified",
    "best_offer": None,
    "best_found": {
        "retailer": "Best Buy",
        "seller": "Best Buy",
        "price": 1049.99,
        "currency": "USD",
        "shipping": None,
        "total_if_known": None,
        "availability": "in_stock",
        "condition": "new",
        "url": "https://www.bestbuy.com/product/example",
        "source_api": "bestbuy_products_api",
        "fetched_at": "2026-10-31T00:00:00Z",
        "match_confidence": 1.0,
        "title": "Samsung Galaxy S25 Ultra 256GB",
        "variant_mismatch": False,
    },
    "offers": [],
    "source_status": {"bestbuy": {"status": "ok", "offers": 1, "filtered_out": 0}},
    "fetched_at": "2026-10-31T00:00:00Z",
    "cache": {"hit": False},
    "notes": [],
}

_PRODUCT_URL = "https://www.example.com/product"

#: Per-tool discovery metadata for the MCP surface. ``input_schema`` mirrors
#: the FastMCP-generated argument schema of each tool in mcp_server.py;
#: ``output_schema`` / ``output_example`` describe the tool's return value.
_BAZAAR_TOOLS: dict[str, dict] = {
    "check_price": {
        "description": "Check the current price of a product page.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "example_args": {"url": _PRODUCT_URL},
        "output_schema": _PRICE_INFO_SCHEMA,
        "output_example": _PRICE_INFO_EXAMPLE,
    },
    "make_affiliate_link": {
        "description": "Build an affiliate tracking link for a product URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "tag": {"type": "string"},
                "program": {"type": "string", "default": "amazon"},
            },
            "required": ["url", "tag"],
        },
        "example_args": {"url": _PRODUCT_URL, "tag": "mytag-20", "program": "amazon"},
        "output_schema": {"type": "string"},
        "output_example": "https://www.example.com/product?tag=mytag-20",
    },
    "find_best_price": {
        "description": (
            "Find the best price for one product across reputable stores "
            "(official retailer APIs + direct stores). Ranked offers with "
            "evidence; a winner is named only on a high-confidence match "
            "with a known delivered total."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "url": {"type": "string", "default": ""},
                "upc": {"type": "string", "default": ""},
                "condition": {"type": "string", "default": "new"},
                "country": {"type": "string", "default": "US"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": [],
        },
        "example_args": {"upc": "887276925174"},
        "output_schema": _BEST_PRICE_SCHEMA,
        "output_example": _BEST_PRICE_EXAMPLE,
    },
}

#: Per-route discovery metadata for the REST surface. Body examples and body
#: schemas mirror the pydantic request models in rest_app.py; output shapes
#: mirror the route handlers' return values.
_BAZAAR_ROUTES: dict[str, dict] = {
    "/price": {
        "tool": "check_price",
        "body_type": "json",
        "body_example": {"url": _PRODUCT_URL},
        "body_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "output_schema": _PRICE_INFO_SCHEMA,
        "output_example": _PRICE_INFO_EXAMPLE,
    },
    "/affiliate": {
        "tool": "make_affiliate_link",
        "body_type": "json",
        "body_example": {"url": _PRODUCT_URL, "tag": "mytag-20", "program": "amazon"},
        "body_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "tag": {"type": "string"},
                "program": {"type": "string"},
            },
            "required": ["url", "tag"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "output_example": {"url": "https://www.example.com/product?tag=mytag-20"},
    },
    "/best-price": {
        "tool": "find_best_price",
        "body_type": "json",
        "body_example": {"upc": "887276925174"},
        "body_schema": {
            "type": "object",
            "properties": {
                "query": {"type": ["string", "null"]},
                "url": {"type": ["string", "null"]},
                "upc": {"type": ["string", "null"]},
                "condition": {"type": "string"},
                "country": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": [],
        },
        "output_schema": _BEST_PRICE_SCHEMA,
        "output_example": _BEST_PRICE_EXAMPLE,
    },
}


def _bazaar_target(
    resource: str, description: str, tool_name: str | None
) -> tuple[str, dict, str] | None:
    """Resolve a requirements resource to its bazaar metadata table entry.

    Returns ``(kind, meta, tool_name)`` — kind ``"rest"`` (meta from
    ``_BAZAAR_ROUTES``) or ``"mcp"`` (meta from ``_BAZAAR_TOOLS``) — or
    None when the resource is not a catalogable endpoint. For /mcp the
    tool comes from the explicit argument or, failing that, from the
    requirements description (``TOOL_DESCRIPTIONS`` values are prefixed
    with the tool name).
    """
    path = urllib.parse.urlsplit(resource).path or resource
    route = _BAZAAR_ROUTES.get(path)
    if route is not None:
        return "rest", route, route["tool"]
    if path == "/mcp" or path.startswith("/mcp/"):
        if tool_name not in _BAZAAR_TOOLS:
            tool_name = next(
                (name for name in _BAZAAR_TOOLS if description.startswith(f"{name}:")),
                None,
            )
        if tool_name in _BAZAAR_TOOLS:
            return "mcp", _BAZAAR_TOOLS[tool_name], str(tool_name)
    return None


def _bazaar_output_schema(
    resource: str, description: str, tool_name: str | None = None
) -> dict | None:
    """x402 v1 ``outputSchema`` for one resource (None = not catalogable).

    For MCP tools/call resources the endpoint is HTTP, so the input is the
    http form whose ``bodyFields`` carry the JSON-RPC envelope example —
    exactly what the v1 bazaar extractor turns into a catalog entry.
    """
    target = _bazaar_target(resource, description, tool_name)
    if target is None:
        return None
    kind, meta, name = target
    if kind == "rest":
        body: dict = copy.deepcopy(meta["body_example"])
    else:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": copy.deepcopy(meta["example_args"])},
        }
    return {
        "input": {
            "type": "http",
            "method": "POST",
            "discoverable": True,
            "bodyType": meta.get("body_type", "json"),
            "bodyFields": body,
        },
        "output": copy.deepcopy(meta["output_schema"]),
    }


def _bazaar_extension(resource: str, description: str, tool_name: str | None = None) -> dict | None:
    """x402 v2 ``extensions.bazaar`` block for one resource (None = skip).

    Mirrors the x402 SDK's declare functions' output in its served
    (post-enrichment) state: ``info.input`` carries the real HTTP method /
    tool name and validates against the sibling ``schema``.
    """
    target = _bazaar_target(resource, description, tool_name)
    if target is None:
        return None
    kind, meta, name = target
    if kind == "rest":
        info_input: dict = {
            "type": "http",
            "method": "POST",
            "bodyType": meta["body_type"],
            "body": copy.deepcopy(meta["body_example"]),
        }
        input_schema_block: dict = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "http"},
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                "body": copy.deepcopy(meta["body_schema"]),
            },
            "required": ["type", "method", "bodyType", "body"],
            "additionalProperties": False,
        }
        output_example_schema: dict = {"type": "object"}
    else:
        info_input = {
            "type": "mcp",
            "toolName": name,
            "description": meta["description"],
            "transport": "streamable-http",
            "inputSchema": copy.deepcopy(meta["input_schema"]),
            "example": copy.deepcopy(meta["example_args"]),
        }
        input_schema_block = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "mcp"},
                "toolName": {"type": "string"},
                "inputSchema": {"type": "object"},
                "description": {"type": "string"},
                "transport": {"type": "string", "enum": ["streamable-http", "sse"]},
                "example": {"type": "object"},
            },
            "required": ["type", "toolName", "inputSchema"],
            "additionalProperties": False,
        }
        output_example_schema = {}
    return {
        "info": {
            "input": info_input,
            "output": {"type": "json", "example": copy.deepcopy(meta["output_example"])},
        },
        "schema": {
            "$schema": _BAZAAR_SCHEMA_URL,
            "type": "object",
            "properties": {
                "input": input_schema_block,
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "example": output_example_schema,
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


def _bazaar_extension_for_requirements(requirements: dict) -> dict | None:
    """``extensions.bazaar`` for a challenge built from these requirements.

    The tool name for /mcp challenges is recovered from the tools/call
    envelope the requirements builder embedded in ``outputSchema`` — the
    requirements dict is the single carrier, so every challenge variant
    (payment_required / payment_invalid / free_tier_exhausted) is covered
    without threading new parameters through the response helpers.
    """
    tool_name: str | None = None
    output_schema = requirements.get("outputSchema")
    if isinstance(output_schema, dict):
        body = (output_schema.get("input") or {}).get("bodyFields")
        if isinstance(body, dict):
            params = body.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                tool_name = params["name"]
    return _bazaar_extension(
        requirements.get("resource", ""),
        requirements.get("description", ""),
        tool_name,
    )


# --------------------------------------------------------------------------
# paymentRequirements (built server-side from env, never client-asserted)


def _requirements_for_config(
    config: dict,
    price: Decimal,
    resource: str,
    description: str,
    tool: str | None = None,
) -> dict:
    requirements = {
        "scheme": SCHEME,
        "network": config["network_id"],
        "maxAmountRequired": str(usdc_to_atomic(price)),
        "resource": resource,
        "description": description,
        "payTo": config["pay_to"],
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "asset": config["asset"],
        # CDP facilitator schema-validates v1 requirements: mimeType is
        # REQUIRED (400 without it, verified live 2026-10-20); extra carries
        # the USDC EIP-712 domain on Base / the fee payer on Solana.
        "mimeType": "application/json",
        "extra": dict(config["extra"]),
    }
    # Bazaar discovery metadata (additive; see the bazaar block above).
    output_schema = _bazaar_output_schema(resource, description, tool)
    if output_schema is not None:
        requirements["outputSchema"] = output_schema
    return requirements


def _base_config() -> dict:
    """The Base leg's config, built from env whether or not it is servable."""
    return {
        "network_id": NETWORK,
        "asset": USDC_BASE_ADDRESS,
        "pay_to": x402_pay_to() or "<set X402_PAY_TO>",
        "decimals": USDC_DECIMALS,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def build_payment_requirements(
    price: Decimal,
    resource: str,
    description: str,
    pay_to: str,
    tool: str | None = None,
) -> dict:
    """Server-side payment requirements for the x402 v1 exact scheme (Base leg)."""
    config = _base_config()
    config["pay_to"] = pay_to or "<set X402_PAY_TO>"
    return _requirements_for_config(config, price, resource, description, tool)


def build_requirements_list(
    price: Decimal,
    resource: str,
    description: str,
    tool: str | None = None,
) -> list[dict]:
    """One requirements entry per servable network leg (plan §4b.2)."""
    return [
        _requirements_for_config(config, price, resource, description, tool)
        for config in network_configs().values()
    ]


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
        # M-1: bound the authorization lifetime so the replay-claim TTL
        # always covers it. Without this, a payer could submit an
        # authorization valid for days while the seen-signature cache evicts
        # after 300s, enabling pay-once/replay-for-free (the facilitator only
        # rejects the spent nonce at /settle, which happens AFTER the
        # response is served).
        window = valid_before - valid_after
        if window <= 0 or window > MAX_TIMEOUT_SECONDS:
            return "authorization validity window exceeds maximum"
        if not str(inner.get("signature", "")).startswith("0x"):
            return "missing signature"
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return f"malformed payment payload: {exc}"
    return None


def sanity_check_solana_failure(payment: dict, requirements: dict) -> str | None:
    """Local sanity checks for a Solana x402 payload (plan §3b).

    Deliberately light: the payload carries a partially-signed transaction,
    not EIP-3009 fields, and deserializing it locally would need a Solana
    codec (new dependency — deferred hardening follow-up). Recipient and
    amount validation is the facilitator's job here, same trust anchor as
    on Base; what we check locally is shape only.
    """
    if payment.get("x402Version") != X402_VERSION:
        return f"unsupported x402Version (want {X402_VERSION})"
    if payment.get("scheme") != SCHEME:
        return f'unsupported scheme (want "{SCHEME}")'
    if payment.get("network") != requirements.get("network"):
        return f'unsupported network (want "{requirements.get("network")}")'
    inner = payment.get("payload")
    if not isinstance(inner, dict):
        return "missing payload object"
    transaction = inner.get("transaction")
    if not isinstance(transaction, str) or not transaction.strip():
        return "missing transaction"
    try:
        # Standard base64 per spec; tolerate the URL-safe alphabet too.
        raw = base64.b64decode(transaction.strip(), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return "invalid transaction encoding"
    if not raw:
        return "invalid transaction encoding"
    return None


def sanity_check_for_network(payment: dict, requirements: dict) -> str | None:
    """Dispatch the local sanity checks on the requirements' network."""
    if requirements.get("network") == SOLANA_NETWORK:
        return sanity_check_solana_failure(payment, requirements)
    return sanity_check_failure(payment, requirements)


def replay_key_for(payment: dict, requirements: dict) -> str:
    """Anti-replay claim key: the 0x signature on EVM; on Solana the base64
    transaction string itself (the spec's recommended mitigation, §3b).
    The "solana:" prefix keeps the shared claim table collision-free."""
    inner = payment["payload"]
    if requirements.get("network") == SOLANA_NETWORK:
        return "solana:" + inner["transaction"]
    return inner["signature"]


# --------------------------------------------------------------------------
# Anti-replay: atomic claims in the billing DB (M-1), in-memory fallback.


_seen_signatures: dict[str, float] = {}
_seen_lock = threading.Lock()


def check_replay(signature: str) -> bool:
    """True if this signature was already used (replay). Records new ones.

    M-1: claims persist in the billing SQLite DB — they survive restarts and
    are shared across workers, closing the in-memory cache's gaps. The claim
    is atomic (INSERT PRIMARY KEY), so concurrent duplicates across the
    facilitator-verify gap collapse to a single serve. Falls back to the
    in-memory dict when the DB is unreachable (availability first; still
    correct within this process).
    """
    try:
        return billing.claim_x402_signature(signature, time.time() + MAX_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — DB failure must not break payments
        log.warning("x402 replay-claim DB unavailable; using in-memory fallback")
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
    try:
        billing.init_db()
        with sqlite3.connect(billing.db_path()) as conn:
            conn.execute("DELETE FROM x402_seen_signatures")
            conn.commit()
    except Exception:  # noqa: BLE001 — test helper; DB absence is fine
        pass


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


def mint_cdp_jwt(
    key_id: str, key_secret: str, now: int | None = None, uris: list[str] | None = None
) -> str:
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
    if uris:
        claims["uris"] = list(uris)
    signing_input = (
        _b64url_nopad(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_nopad(json.dumps(claims, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = key.sign(signing_input)
    return signing_input.decode("ascii") + "." + _b64url_nopad(signature)


def _facilitator_headers(base_url: str | None = None) -> dict:
    """Auth headers for the facilitator, per env config (pluggable).

    CDP JWTs are scoped (``uris`` claim) to the facilitator base actually
    being called, so the Solana leg's dedicated URL gets its own scope.
    """
    api_key = os.environ.get("X402_FACILITATOR_API_KEY", "").strip()
    if api_key:
        return {"X-API-Key": api_key}
    key_id = os.environ.get("CDP_API_KEY_ID", "").strip()
    key_secret = os.environ.get("CDP_API_KEY_SECRET", "").strip()
    if key_id and key_secret:
        base = (base_url or x402_facilitator_url()).split("://", 1)[-1].rstrip("/")
        uris = [f"POST {base}/verify", f"POST {base}/settle"]
        return {"Authorization": f"Bearer {mint_cdp_jwt(key_id, key_secret, uris=uris)}"}
    return {}


# --------------------------------------------------------------------------
# Facilitator client (x402 v1 "exact" scheme)


class FacilitatorUnavailable(Exception):
    """The facilitator could not be reached or answered badly. Fail closed."""


def _facilitator_base(requirements: dict | None = None) -> str:
    """Per-network facilitator routing (plan §4b.4): the Solana leg uses
    X402_FACILITATOR_URL_SOLANA when set, else the shared facilitator."""
    if requirements is not None and requirements.get("network") == SOLANA_NETWORK:
        return solana_facilitator_url().rstrip("/")
    return x402_facilitator_url().rstrip("/")


async def resolve_solana_fee_payer() -> str | None:
    """Best-effort feePayer discovery from GET {facilitator}/supported (§3b).

    Cached on success. Any failure returns None (the Solana leg then drops
    with an ERROR log) — discovery must never crash a request.
    """
    global _solana_fee_payer_cache
    known = solana_fee_payer()
    if known:
        return known
    url = solana_facilitator_url().rstrip("/") + "/supported"
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=_facilitator_headers(solana_facilitator_url()))
        data = resp.json()
    except Exception:
        log.warning("x402 solana fee payer discovery failed: %s unreachable", url)
        return None
    kinds = data.get("kinds") if isinstance(data, dict) else None
    if isinstance(kinds, list):
        for kind in kinds:
            if not isinstance(kind, dict):
                continue
            if kind.get("scheme") != SCHEME:
                continue
            network = str(kind.get("network", ""))
            if not network.startswith(SOLANA_NETWORK):
                continue
            extra = kind.get("extra") or {}
            fee_payer = extra.get("feePayer")
            if isinstance(fee_payer, str) and fee_payer:
                _solana_fee_payer_cache = fee_payer
                return fee_payer
    log.warning("x402 facilitator /supported lists no Solana fee payer")
    return None


async def facilitator_verify(
    payment_payload: dict, requirements: dict
) -> tuple[bool, str | None, str | None]:
    """POST {facilitator}/verify. Returns (is_valid, invalid_reason, payer)."""
    body = {
        "x402Version": X402_VERSION,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    base = _facilitator_base(requirements)
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}/verify", json=body, headers=_facilitator_headers(base)
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
    base = _facilitator_base(requirements)
    try:
        async with httpx.AsyncClient(timeout=FACILITATOR_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}/settle", json=body, headers=_facilitator_headers(base)
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


# ---------------------------------------------------------------------------
# Canonical 402 challenge body.
# ---------------------------------------------------------------------------
# This exact shape is shared by all IOConnex MCP servers (search-mcp,
# doc-convert-mcp, price-tracker-mcp): the same ``error`` codes, the same
# ``accepts`` fields, and ``reason`` on every payment rejection. ``accepts``
# is derived from the x402 ``paymentRequirements`` so the two can never drift
# apart. Only OUR extension fields are normalized here — the
# ``paymentRequirements`` object itself stays x402-protocol-compliant.

CHALLENGE_MESSAGE = (
    "This endpoint requires an x402 USDC micropayment on Base. "
    "Send an X-Payment header with a base64-encoded signed EIP-3009 "
    "authorization (x402 v1 'exact' scheme)."
)

CHALLENGE_MESSAGE_MULTI = (
    "This endpoint requires an x402 USDC micropayment (Base or Solana). "
    "Send an X-Payment header with a base64-encoded x402 v1 'exact' "
    "scheme payment for one of the accepted networks."
)

FACILITATOR_DOWN_MESSAGE = "Payment verification is temporarily unavailable; try again."


def _accepts_entry(requirements: dict) -> dict:
    amount_atomic = int(requirements["maxAmountRequired"])
    return {
        "asset": "USDC",
        "asset_contract": requirements["asset"],
        "network": requirements["network"],
        "amount_usdc": str(Decimal(amount_atomic) / 10**USDC_DECIMALS),
        "amount_atomic": str(amount_atomic),
        "pay_to": requirements["payTo"],
        "scheme": requirements["scheme"],
    }


def challenge_content(
    error: str,
    requirements: dict | list[dict],
    reason: str | None = None,
    message: str | None = None,
) -> dict:
    """Build the canonical 402 body for an x402 payment rejection.

    ``error`` is one of ``payment_required`` / ``payment_invalid`` /
    ``duplicate_payment`` (or a repo-specific 402 code like
    ``free_tier_exhausted``). ``reason`` carries the human/machine-readable
    detail when known; ``message`` overrides the default challenge text when
    the caller has something more specific to say.

    ``requirements`` is a single requirements dict (the original shape,
    unchanged) or a list of per-network dicts. A one-entry list collapses
    to the single shape; with more than one network, ``accepts`` becomes
    an ARRAY of per-network options and ``paymentRequirements`` the
    matching array (plan §4b.3).
    """
    if isinstance(requirements, list):
        if len(requirements) == 1:
            requirements = requirements[0]
        else:
            content_multi: dict = {
                "error": error,
                "message": message or CHALLENGE_MESSAGE_MULTI,
                "accepts": [_accepts_entry(entry) for entry in requirements],
                # Real x402 clients need this to build a valid payment.
                "paymentRequirements": requirements,
            }
            # Bazaar discovery metadata (additive; one block describes the
            # resource — identical across network legs).
            extension_multi = _bazaar_extension_for_requirements(requirements[0])
            if extension_multi is not None:
                content_multi["extensions"] = {"bazaar": extension_multi}
            if reason:
                content_multi["reason"] = reason
            return content_multi
    content: dict = {
        "error": error,
        "message": message or CHALLENGE_MESSAGE,
        "accepts": _accepts_entry(requirements),
        # Real x402 clients need this to build a valid payment.
        "paymentRequirements": requirements,
    }
    # Bazaar discovery metadata (additive; see the bazaar block above).
    extension = _bazaar_extension_for_requirements(requirements)
    if extension is not None:
        content["extensions"] = {"bazaar": extension}
    if reason:
        content["reason"] = reason
    return content


def payment_required_response(
    requirements: dict | list[dict], reason: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=402,
        content=challenge_content("payment_required", requirements, reason),
    )


def payment_invalid_response(
    requirements: dict | list[dict], code: str, reason: str
) -> JSONResponse:
    return JSONResponse(status_code=402, content=challenge_content(code, requirements, reason))


def free_tier_exhausted_response(usage: dict, requirements: dict | list[dict]) -> JSONResponse:
    """HTTP 402 when the monthly free credit bucket is exhausted.

    Deliberately 402, NOT 429: 429 stays exclusive to the subscriber
    fair-use cap (``subscriber_monthly_cap_exceeded``). The body carries the
    same canonical x402 payment challenge so the caller can pay per call
    immediately, plus the free-tier usage fields.
    """
    content = challenge_content(
        "free_tier_exhausted",
        requirements,
        message=(
            f"Free tier exhausted: {usage['credits_used']} of {usage['free_limit']} "
            f"free credits used this month (resets {usage['reset_at']}). "
            "Pay per call with x402 or subscribe for $9/mo ($10 of calls included)."
        ),
    )
    content = challenge_content(
        "free_tier_exhausted",
        requirements,
        message=(
            f"Free tier exhausted: {usage['credits_used']} of {usage['free_limit']} "
            f"free credits used this month (resets {usage['reset_at']}). "
            "Pay per call with x402 or subscribe for $9/mo ($10 of calls included)."
        ),
    )
    content.update(
        {
            "credits_used": usage["credits_used"],
            "free_limit": usage["free_limit"],
            "resets_at": usage["reset_at"],
        }
    )
    return JSONResponse(status_code=402, content=content)


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
# Free tier + usage analytics helpers


# Cloudflare edge IP ranges, hardcoded 2026-10-01 from
# https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6.
# Single source of truth: rest_app imports these (x402 must not import
# rest_app — the dependency runs the other way). Refresh these if Cloudflare
# publishes an update; stale ranges would silently disable the
# CF-Connecting-IP fast path below (failing safe to the peer, no hole).
_CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        # IPv6
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _peer_is_cloudflare(peer: str) -> bool:
    """True when the direct TCP peer is a Cloudflare edge IP."""
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _CLOUDFLARE_NETWORKS)


def client_ip(request: Request) -> str:
    """Client IP for the monthly free credit bucket, proxy-aware (M-3).

    Render terminates TLS at its proxy and appends the original client
    address to ``X-Forwarded-For``; the first entry is trusted ONLY when the
    direct TCP peer is a non-global address (i.e. we really are behind the
    proxy). A client connecting directly always has a global peer address, so
    a spoofed XFF from a direct connection is ignored — and the bucket can no
    longer be reset at will by rotating the header. An unparseable peer (the
    TestClient's ``"testclient"``) means a test harness, where XFF is trusted
    as before. Falls back to the socket peer when the header is absent (local
    dev / tests). The bucket stores only the SHA-256 of this value, never the
    raw IP.

    When the domain is proxied through Cloudflare, the direct TCP peer is a
    Cloudflare edge IP and ``CF-Connecting-IP`` carries the real client
    address — it is trusted ONLY for verifiably Cloudflare peers, so spoofing
    from any other peer is impossible. Without the header we fall back to the
    peer (never XFF: Cloudflare appends to XFF, so its first entry is
    client-spoofable).
    """
    peer = request.client.host if request.client else ""
    if _peer_is_cloudflare(peer):
        # Proxied through Cloudflare: CF-Connecting-IP carries the real
        # client IP. Honored ONLY when the TCP peer is verifiably a
        # Cloudflare edge IP.
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            try:
                ipaddress.ip_address(cf_ip)
                return cf_ip
            except ValueError:
                pass  # garbage header: fall through to existing logic
    xff = request.headers.get("x-forwarded-for", "")
    try:
        behind_proxy = not ipaddress.ip_address(peer).is_global
    except ValueError:
        behind_proxy = True  # test harness (e.g. TestClient): no real peer
    if behind_proxy and xff:
        first = xff.split(",")[0].strip()
        if first:
            try:
                ipaddress.ip_address(first)
                return first
            except ValueError:
                pass  # garbage XFF value: fall through to the peer
    return peer or "unknown"


async def _classify_request(request: Request) -> dict:
    """Classify a request for metering + usage analytics.

    Mirrors ``_resolve_price`` for pricing (same price map, same exemptions)
    and adds a stable ``tool`` label for the analytics rollups. ``/billing``
    paths never reach here (dispatch skips them before metering).

    Returns ``{"priced": True, "price", "description", "tool"}`` for metered
    calls or ``{"priced": False, "tool"}`` for free discovery calls (counted
    in analytics as free/0).
    """
    path = request.url.path
    if path.startswith("/mcp") and request.method == "POST":
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            price, description = _default_price("mcp request")
            return {
                "priced": True,
                "price": price,
                "description": description,
                "tool": "mcp_request",
            }
        method = body.get("method")
        if method in JSONRPC_DISCOVERY_METHODS or (
            isinstance(method, str) and method.startswith("notifications/")
        ):
            return {"priced": False, "tool": "mcp_discovery"}
        if method == "tools/call":
            params = body.get("params")
            tool = params.get("name") if isinstance(params, dict) else None
            if isinstance(tool, str) and tool in TOOL_PRICE_DEFAULTS:
                return {
                    "priced": True,
                    "price": tool_price_usdc(tool),
                    "description": TOOL_DESCRIPTIONS[tool],
                    "tool": tool,
                }
            price, description = _default_price(f"mcp tools/call {tool!r}")
            return {
                "priced": True,
                "price": price,
                "description": description,
                "tool": "mcp_tools/call",
            }
        price, description = _default_price(f"mcp {method}")
        label = f"mcp:{method}" if isinstance(method, str) else "mcp_request"
        return {"priced": True, "price": price, "description": description, "tool": label}
    tool = REST_TOOL_MAP.get(path)
    if tool is not None:
        return {
            "priced": True,
            "price": tool_price_usdc(tool),
            "description": TOOL_DESCRIPTIONS[tool],
            "tool": tool,
        }
    price, description = _default_price(path)
    return {"priced": True, "price": price, "description": description, "tool": f"rest:{path}"}


def _safe_record(tool: str, plan_type: str, credits: float) -> None:
    """Roll one served call into usage analytics. Never breaks serving.

    Analytics is best-effort observability: a DB hiccup is logged and the
    request still serves.
    """
    try:
        billing.record_usage_rollup(tool, plan_type, credits)
    except Exception:  # noqa: BLE001
        log.warning("usage analytics record failed", exc_info=True)


# --------------------------------------------------------------------------
# Middleware


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Metering: subscriber key -> x402 per-call -> monthly free bucket.

    Order per request (all at this one hook point, REST + MCP alike):
      1. valid ``X-Subscriber-Key`` -> subscriber path (monthly credit cap,
         429 ``subscriber_monthly_cap_exceeded`` — untouched by the free tier);
      2. ``X-Payment`` present -> x402 verify/settle flow (exempt from every
         cap, including the free bucket);
      3. else -> monthly free credit bucket keyed by client IP (402
         ``free_tier_exhausted`` when spent; ``FREE_TIER_ENABLED=false``
         skips straight to the x402 402 challenge);
      4. exempt/discovery paths serve free; ``/billing/*`` is never
         metered and never counted.

    Every served call on a metered path is rolled into ``usage_daily``
    (free/subscriber/x402), best-effort — analytics never breaks serving.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES):
            return await call_next(request)
        if path.startswith("/billing"):
            # Billing/admin flow has its own enablement logic: never
            # x402-gated, never free-tiered, never counted in analytics.
            return await call_next(request)
        if path.startswith("/mcp") and request.method == "GET":
            # MCP discovery/info GETs stay free; counted as free/0.
            _safe_record("mcp_discovery", "free", 0.0)
            return await call_next(request)
        if not x402_effective_enabled():
            # Payments off: everything serves; counted as free/0.
            classified = await _classify_request(request)
            _safe_record(classified["tool"], "free", 0.0)
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
            resolved = await _resolve_price(request)
            classified = await _classify_request(request)
            cost = price_to_credits(resolved[0]) if resolved is not None else 0.0
            usage = billing.record_subscriber_usage(sub_key, cost)
            if not usage["allowed"]:
                return subscriber_limit_response(usage)
            response = await call_next(request)
            _safe_record(classified["tool"], "subscriber", cost)
            return response

        classified = await _classify_request(request)
        if not classified["priced"]:
            # JSON-RPC discovery: free, counted as free/0.
            response = await call_next(request)
            _safe_record(classified["tool"], "free", 0.0)
            return response

        price, description, tool = (
            classified["price"],
            classified["description"],
            classified["tool"],
        )
        # Solana fee payer discovery is lazy (plan §3b): resolve it before
        # building the challenge so the Solana leg can be offered at all.
        if SOLANA_NETWORK in requested_networks() and not solana_fee_payer():
            await resolve_solana_fee_payer()

        requirements_list = build_requirements_list(price, str(request.url), description, tool=tool)
        if not requirements_list:
            # x402 is enabled but no leg is servable right now (e.g. the
            # Solana fee payer could not be resolved): fail closed rather
            # than serve a paid endpoint for free.
            log.error("x402 enabled but no servable network legs — failing closed")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "payment_verification_unavailable",
                    "message": FACILITATOR_DOWN_MESSAGE,
                },
            )
        requirements_by_network = {entry["network"]: entry for entry in requirements_list}
        # Single-network callers (and the no-X-Payment challenge below) keep
        # the original single-requirements shape via the collapsing list.
        requirements = requirements_list[0]

        raw = request.headers.get("x-payment")
        if raw:
            payment = decode_x_payment(raw)
            if payment is None:
                return payment_required_response(
                    requirements_list, reason="X-Payment header is not valid base64-encoded JSON"
                )
            # Select the requirements for the network the client actually
            # paid on (plan §4b.4); an unknown network is rejected outright.
            network = payment.get("network")
            leg = requirements_by_network.get(network) if isinstance(network, str) else None
            if leg is None and network is None:
                # No network asserted: fall back to the Base requirements
                # so the EVM sanity checks report the mismatch, as before.
                leg = requirements_by_network.get(NETWORK)
            if leg is None and len(requirements_list) == 1:
                # Single-network configs keep their exact pre-port behavior:
                # the one servable leg's sanity checks report the mismatch
                # (e.g. 'unsupported network (want "base")'), as before.
                leg = requirements_list[0]
            if leg is None:
                return payment_invalid_response(
                    requirements_list, "payment_invalid", "unsupported network"
                )
            requirements = leg
            failure = sanity_check_for_network(payment, requirements)
            if failure is not None:
                return payment_invalid_response(requirements, "payment_invalid", failure)
            replay_key = replay_key_for(payment, requirements)
            if check_replay(replay_key):
                return payment_invalid_response(
                    requirements_list,
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
                        "message": FACILITATOR_DOWN_MESSAGE,
                    },
                )
            if not is_valid:
                return payment_invalid_response(
                    requirements,
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
            # x402 payers are recorded at list price: the credits that WOULD
            # have been charged.
            _safe_record(tool, "x402", price_to_credits(price))
            return response

        # No X-Payment: the monthly free bucket. Denied/exhausted calls never
        # consume credits; the deduction happens here, before serving, exactly
        # like the subscriber cap.
        # Always-paid aliases never draw from the free bucket: without
        # a valid X-Payment they fall through to the 402 challenge
        # below, so directory checkers and router dispatch see a price.
        if billing.free_tier_enabled() and not path.startswith(PAID_ROUTE_PREFIX):
            cost = price_to_credits(price)
            usage = billing.record_free_tier_usage(client_ip(request), cost)
            if not usage["allowed"]:
                return free_tier_exhausted_response(usage, requirements_list)
            response = await call_next(request)
            _safe_record(tool, "free", cost)
            return response

        # Free tier killed: straight to the x402 402 challenge.
        return payment_required_response(requirements_list)


# --------------------------------------------------------------------------
# Startup


def warn_if_payments_live() -> None:
    if x402_enabled() and not x402_effective_enabled():
        log.error(
            "X402_ENABLED=true but X402_PAY_TO / X402_FACILITATOR_URL incomplete — "
            "x402 DISABLED (fail closed). Set the missing config or flip the flag off."
        )
    elif x402_effective_enabled():
        servable = valid_networks()
        if NETWORK in servable:
            log.warning(
                "X402 payments LIVE — real USDC charges on Base. "
                "Confirm AZ LLC is formed before the first dollar arrives."
            )
        if SOLANA_NETWORK in servable:
            if solana_facilitator_url() == DEFAULT_FACILITATOR_URL:
                log.warning(
                    "X402 Solana leg LIVE against %s — that default facilitator is "
                    "Base Sepolia only; Solana needs the CDP facilitator + keys.",
                    DEFAULT_FACILITATOR_URL,
                )
            else:
                log.warning(
                    "X402 payments LIVE — real USDC charges on Solana. "
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
    "build_requirements_list",
    "decode_x_payment",
    "sanity_check_failure",
    "sanity_check_solana_failure",
    "sanity_check_for_network",
    "replay_key_for",
    "resolve_solana_fee_payer",
    "network_configs",
    "valid_networks",
    "requested_networks",
    "x402_pay_to_solana",
    "solana_facilitator_url",
    "check_replay",
    "mint_cdp_jwt",
    "facilitator_verify",
    "facilitator_settle",
    "FacilitatorUnavailable",
    "payment_required_response",
    "payment_invalid_response",
    "subscriber_limit_response",
    "free_tier_exhausted_response",
    "client_ip",
    "price_to_credits",
    "build_discovery_manifest",
    "USDC_BASE_ADDRESS",
    "TOOL_PRICE_DEFAULTS",
]
