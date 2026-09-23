"""Stripe billing: fiat monthly plans with a subscriber-key x402 bypass.

Model: per-call x402 micropayments stay the default path; a Stripe monthly
subscription ("Pro Monthly") buys a subscriber key that bypasses per-call
charges (fair use).

Endpoints (all 404 unless Stripe is effectively enabled):

- ``POST /billing/checkout`` -> Stripe Checkout Session (mode=subscription)
  for ``STRIPE_PRICE_ID``; returns ``{"checkout_url": ...}``.
- ``POST /billing/stripe-webhook`` -> Stripe webhook handler
  (``stripe.Webhook.construct_event`` with ``STRIPE_WEBHOOK_SECRET``):
  ``checkout.session.completed`` -> create subscriber row;
  ``customer.subscription.deleted`` -> deactivate all keys for the customer;
  ``customer.subscription.updated`` -> sync status.
- ``GET /billing/key?session_id=...`` -> verifies the session is paid with
  Stripe, then creates and returns a fresh subscriber token (shown once;
  only its SHA-256 is stored — old keys for the customer stay valid until
  the subscription is cancelled).
- ``GET /billing/status`` -> ``{"stripe_enabled": bool, "x402_enabled": bool}``
  (effective state only, never secret values).

The middleware checks ``X-Subscriber-Key`` -> sha256 -> active row -> bypass.
Invalid/expired keys fall through to the normal x402 flow (not a hard error).

Fair use: Pro subscriptions include ``SUBSCRIBER_MONTHLY_CALL_LIMIT`` calls
per calendar month (UTC; default 1000 — Scott's call, see PR body). Usage is
tracked per subscriber-key hash in the same SQLite DB; the counter rolls over
on the month boundary. Over the cap -> HTTP 429 with an actionable body (the
cap, usage, reset date, and the x402 per-call fallback) — never a silent
serve. The cap applies to subscriber-key auth only, not x402 per-call payers.

Storage: stdlib ``sqlite3`` at ``BILLING_DB_PATH`` (default
``/var/lib/price-tracker-mcp/billing.db`` — a Render persistent-disk mount in
production; override per-env as needed); the parent directory is created on
write, and table creation is idempotent (``IF NOT EXISTS``) so first boot on
a fresh disk works. Never ``./data`` in production: Render's filesystem is
ephemeral, so a relative path would wipe paid subscriber keys on redeploy.
Payments OFF by default: ``stripe_effective_enabled()`` requires
``STRIPE_ENABLED=true`` AND ``STRIPE_SECRET_KEY`` AND ``STRIPE_WEBHOOK_SECRET``
AND ``STRIPE_PRICE_ID``. Flag on but config incomplete -> ERROR at startup,
treated as disabled (fail closed).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path

import stripe
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("price_tracker.billing")

router = APIRouter()

# Production default: a Render persistent-disk mount declared in render.yaml
# (``disk:`` stanza, mountPath ``/var/lib/price-tracker-mcp``). Kept as the
# code default so a fresh disk boots cleanly; ``BILLING_DB_PATH`` still
# overrides it when set explicitly (tests, local dev).
DEFAULT_DB_PATH = "/var/lib/price-tracker-mcp/billing.db"

_SCHEMA = """CREATE TABLE IF NOT EXISTS subscribers (
    key_hash TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    email TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""

_USAGE_SCHEMA = """CREATE TABLE IF NOT EXISTS subscriber_usage (
    key_hash TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0
)"""

# Monthly fair-use call cap for the Pro subscription. Business decision —
# this is Scott's call to tune. Rationale for 1000: at ~$0.02 avg per-call
# price, the $9/mo plan breaks even at ~450 calls; 1000 gives headroom.
# Overridable per environment via SUBSCRIBER_MONTHLY_CALL_LIMIT.
DEFAULT_MONTHLY_CALL_LIMIT = 1000


def _env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def stripe_flag_on() -> bool:
    return _env_flag("STRIPE_ENABLED")


def _stripe_secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def _stripe_webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def _stripe_price_id() -> str:
    return os.environ.get("STRIPE_PRICE_ID", "").strip()


def stripe_effective_enabled() -> bool:
    """True only when the flag is on AND all Stripe credentials are set."""
    return (
        stripe_flag_on()
        and bool(_stripe_secret_key())
        and bool(_stripe_webhook_secret())
        and bool(_stripe_price_id())
    )


def db_path() -> Path:
    """Resolved subscriber DB path.

    Default is the Render persistent-disk mount (see ``render.yaml`` ``disk:``
    stanza). Explicitly setting ``BILLING_DB_PATH`` overrides it exactly as
    before — tests and local dev keep doing that.
    """
    return Path(os.environ.get("BILLING_DB_PATH", DEFAULT_DB_PATH))


def init_db() -> Path:
    """Create the DB (and parent dir) if missing. Write path only.

    Idempotent: tables are created ``IF NOT EXISTS`` so first boot on a
    fresh persistent disk works and later boots are no-ops.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(_SCHEMA)
        conn.execute(_USAGE_SCHEMA)
        conn.commit()
    return path


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_subscriber(customer_id: str, email: str | None) -> str:
    """Create a subscriber row; returns the raw token (shown to the user once).

    Only the SHA-256 of the token is stored — the raw token is never persisted.
    """
    token = secrets.token_hex(32)  # 256-bit token, 64 hex chars
    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    init_db()
    now = _now_iso()
    with sqlite3.connect(db_path()) as conn:
        conn.execute(
            "INSERT INTO subscribers "
            "(key_hash, customer_id, email, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (key_hash, customer_id, email, now, now),
        )
        conn.commit()
    log.info("subscriber created for customer %s", customer_id)
    return token


def set_customer_status(customer_id: str, status: str) -> int:
    """Set status for all keys of a customer. Returns rows updated."""
    init_db()
    with sqlite3.connect(db_path()) as conn:
        cur = conn.execute(
            "UPDATE subscribers SET status = ?, updated_at = ? WHERE customer_id = ?",
            (status, _now_iso(), customer_id),
        )
        conn.commit()
        return cur.rowcount


def get_subscriber_rows(customer_id: str) -> list[dict]:
    """Read-only lookup of a customer's rows (for tests/admin tooling)."""
    path = db_path()
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT key_hash, customer_id, email, status, created_at, updated_at "
            "FROM subscribers WHERE customer_id = ?",
            (customer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def valid_subscriber_key(key: str) -> bool:
    """True iff the sha256 of ``key`` matches an active subscriber row."""
    if not key:
        return False
    path = db_path()
    if not path.exists():
        return False
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT status FROM subscribers WHERE key_hash = ?", (key_hash,)
            ).fetchone()
    except sqlite3.Error:
        log.exception("subscriber DB read failed")
        return False
    return bool(row and row[0] == "active")


# --------------------------------------------------------------------------
# Fair-use metering (per subscriber-key hash, calendar month UTC)


def subscriber_monthly_call_limit() -> int:
    """Resolved monthly fair-use cap: env override -> baked-in default."""
    raw = os.environ.get("SUBSCRIBER_MONTHLY_CALL_LIMIT", "").strip()
    if not raw:
        return DEFAULT_MONTHLY_CALL_LIMIT
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "Invalid SUBSCRIBER_MONTHLY_CALL_LIMIT=%r; using default %d",
            raw,
            DEFAULT_MONTHLY_CALL_LIMIT,
        )
        return DEFAULT_MONTHLY_CALL_LIMIT
    if value < 0:
        log.warning(
            "Negative SUBSCRIBER_MONTHLY_CALL_LIMIT=%r; using default %d",
            raw,
            DEFAULT_MONTHLY_CALL_LIMIT,
        )
        return DEFAULT_MONTHLY_CALL_LIMIT
    return value


def current_month() -> str:
    """Current calendar month in UTC as ``YYYY-MM``.

    Small and deliberately patchable: tests pin the month to simulate
    rollover without touching the clock.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def month_reset_at_iso(month: str) -> str:
    """First of the month after ``month`` at 00:00 UTC, as an ISO-8601 Z string."""
    year, mon = (int(part) for part in month.split("-", 1))
    if mon == 12:
        year, mon = year + 1, 1
    else:
        mon += 1
    return f"{year:04d}-{mon:02d}-01T00:00:00Z"


def record_subscriber_call(key: str) -> dict:
    """Check + count one subscriber-key-authenticated call.

    Tracks usage per subscriber-key hash (SHA-256, never the raw key) in the
    same SQLite DB; the counter rolls over on the calendar-month boundary
    (UTC). Calls that would exceed the cap are rejected and NOT counted.

    Call only for keys already validated by ``valid_subscriber_key``.

    Returns ``{"allowed": bool, "usage": int, "limit": int, "reset_at": str}``.

    Concurrency note: the read-modify-write is serialized by SQLite's
    single-writer lock, so exact cap enforcement under heavy concurrency is
    best-effort (a burst racing the boundary may admit one extra call).
    """
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    init_db()
    month = current_month()
    limit = subscriber_monthly_call_limit()
    with sqlite3.connect(db_path()) as conn:
        row = conn.execute(
            "SELECT month, calls FROM subscriber_usage WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        if row is None or row[0] != month:
            # First call this month (or ever): start a fresh counter.
            if 1 <= limit:
                conn.execute(
                    "INSERT INTO subscriber_usage (key_hash, month, calls) "
                    "VALUES (?, ?, 1) ON CONFLICT(key_hash) DO UPDATE SET "
                    "month = excluded.month, calls = excluded.calls",
                    (key_hash, month),
                )
                conn.commit()
                usage, allowed = 1, True
            else:
                # Cap is zero: reject before anything is counted.
                usage, allowed = 0, False
        elif row[1] >= limit:
            # Over the cap: reject without counting this attempt.
            usage, allowed = row[1], False
        else:
            usage, allowed = row[1] + 1, True
            conn.execute(
                "UPDATE subscriber_usage SET calls = ? WHERE key_hash = ?",
                (usage, key_hash),
            )
            conn.commit()
    return {
        "allowed": allowed,
        "usage": usage,
        "limit": limit,
        "reset_at": month_reset_at_iso(month),
    }


def _to_plain_dict(obj):
    """stripe-python v15+ returns StripeObjects (no .get()); normalize to dict."""
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001
            return {}
    return obj if isinstance(obj, dict) else {}


def _require_enabled() -> None:
    if not stripe_effective_enabled():
        raise HTTPException(status_code=404, detail="billing not enabled")


def _stripe_api() -> None:
    # stripe.api_key is process-global; set from env on every call so tests
    # and env changes are honored. Never logged.
    stripe.api_key = _stripe_secret_key()


# --------------------------------------------------------------------------
# Routes


@router.post("/billing/checkout")
def billing_checkout():
    _require_enabled()
    success_url = os.environ.get("STRIPE_SUCCESS_URL", "").strip()
    cancel_url = os.environ.get("STRIPE_CANCEL_URL", "").strip()
    if not success_url or not cancel_url:
        log.error("STRIPE_SUCCESS_URL / STRIPE_CANCEL_URL missing — checkout disabled")
        raise HTTPException(status_code=503, detail="billing checkout not fully configured")
    _stripe_api()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": _stripe_price_id(), "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except stripe.error.StripeError as exc:
        log.exception("Stripe checkout session creation failed")
        raise HTTPException(status_code=502, detail="stripe checkout failed") from exc
    return {"checkout_url": session["url"]}


@router.post("/billing/stripe-webhook")
async def stripe_webhook(request: Request):
    _require_enabled()
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, _stripe_webhook_secret())
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        log.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="bad webhook signature") from exc

    event = _to_plain_dict(event)
    event_type = event.get("type")
    data = event.get("data") or {}
    obj = data.get("object") or {}
    customer_id = obj.get("customer")

    if event_type == "checkout.session.completed" and customer_id:
        details = obj.get("customer_details") or {}
        email = details.get("email") if isinstance(details, dict) else None
        create_subscriber(customer_id, email)
    elif event_type == "customer.subscription.deleted" and customer_id:
        set_customer_status(customer_id, "canceled")
    elif event_type == "customer.subscription.updated" and customer_id:
        sub_status = obj.get("status")
        mapped = "active" if sub_status in ("active", "trialing") else (sub_status or "unknown")
        set_customer_status(customer_id, mapped)
    return {"received": True}


@router.get("/billing/key")
def billing_key(session_id: str):
    """Exchange a completed checkout session for a subscriber token (shown once)."""
    _require_enabled()
    _stripe_api()
    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["customer"])
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail="unknown checkout session") from exc
    session = _to_plain_dict(session)
    if session.get("status") != "complete" or session.get("payment_status") != "paid":
        raise HTTPException(status_code=402, detail="checkout session not paid")
    customer = session.get("customer")
    customer_id = customer.get("id") if isinstance(customer, dict) else customer
    if not customer_id:
        raise HTTPException(status_code=400, detail="checkout session has no customer")
    details = session.get("customer_details") or {}
    email = details.get("email") if isinstance(details, dict) else None
    token = create_subscriber(customer_id, email)
    return {
        "subscriber_key": token,
        "note": (
            "Store this key — it is shown once. Send it as the X-Subscriber-Key "
            "header to bypass per-call x402 charges while your subscription is active."
        ),
    }


@router.get("/billing/status")
def billing_status():
    from .x402 import x402_effective_enabled

    return {
        "stripe_enabled": stripe_effective_enabled(),
        "x402_enabled": x402_effective_enabled(),
    }
