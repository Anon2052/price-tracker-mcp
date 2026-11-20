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
- ``GET /billing/usage`` -> fair-use credit usage for the ``X-Subscriber-Key``
  caller: ``{"credits_used", "credit_limit", "resets_at", "period"}``. Read-only
  (never deducts, never 429-gated, never x402-gated); 401 for missing/invalid
  keys.

The middleware checks ``X-Subscriber-Key`` -> sha256 -> active row -> bypass.
Invalid/expired keys fall through to the normal x402 flow (not a hard error).

Fair use: Pro subscriptions include ``SUBSCRIBER_MONTHLY_CREDIT_LIMIT``
credits per calendar month (UTC; default 1000 credits = $10 of API calls for
$9/mo — Scott's call, see PR body). Credits price calls by their x402 list
price: 1 credit = $0.01, so a $0.025 tool costs 2.5 credits and a $0.005 tool
costs 0.5. Usage is tracked per subscriber-key hash in the same SQLite DB;
the counter rolls over on the month boundary. Over the cap -> HTTP 429 with
an actionable body (the cap, usage, reset date, and the x402 per-call
fallback) — never a silent serve. The cap applies to subscriber-key auth
only, not x402 per-call payers.

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

Free tier: when x402 is enabled, anonymous callers (no subscriber key, no
X-Payment) draw from a monthly free credit bucket keyed by client IP
(``FREE_MONTHLY_CREDITS``, default 50 — Scott's call; kill switch
``FREE_TIER_ENABLED``). Exhaustion -> HTTP 402 ``free_tier_exhausted``
(429 stays exclusive to the subscriber cap). IPs are stored as SHA-256
hashes, never raw.

Usage analytics: every served call on a metered path is rolled up into the
``usage_daily`` table (date/tool/plan_type aggregates — no PII, no IPs):
``GET /billing/stats`` (``ADMIN_KEY``-gated) reports per-day and total
usage by plan type. The stats endpoint is un-metered.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import math
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
    credits REAL NOT NULL DEFAULT 0
)"""

# M-1: persistent x402 anti-replay claims. Survives restarts and is shared
# across workers, closing the gaps of the old in-memory signature cache.
_REPLAY_SCHEMA = """CREATE TABLE IF NOT EXISTS x402_seen_signatures (
    signature TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
)"""


def _ensure_usage_columns() -> None:
    """Add the ``credits`` column to pre-credit ``subscriber_usage`` tables.

    Tables created by the call-based fair-use build only have ``calls``; the
    credit rebuild reads/writes ``credits``. Payments never went live, so no
    data migration is needed — just make sure the column exists.
    """
    with sqlite3.connect(db_path()) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriber_usage)")}
        if "credits" not in columns:
            conn.execute("ALTER TABLE subscriber_usage ADD COLUMN credits REAL NOT NULL DEFAULT 0")
            conn.commit()


# Monthly fair-use credit cap for the Pro subscription. Business decision —
# this is Scott's call to tune. Rationale for 1000: $9/mo buys $10 of API
# calls at x402 list prices (1 credit = $0.01), so a $0.025 tool burns
# 2.5 credits per call. Overridable per environment via
# SUBSCRIBER_MONTHLY_CREDIT_LIMIT.
DEFAULT_MONTHLY_CREDIT_LIMIT = 1000.0


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
        conn.execute(_FREE_TIER_SCHEMA)
        conn.execute(_USAGE_DAILY_SCHEMA)
        conn.execute(_REPLAY_SCHEMA)
        conn.commit()
    _ensure_usage_columns()
    return path


def claim_x402_signature(signature: str, expires_at: float) -> bool:
    """Atomically claim an x402 payment signature (M-1 anti-replay).

    Returns True when the signature was already claimed and has not expired
    (a replay); False when this call claimed it. The claim is atomic
    (INSERT PRIMARY KEY): concurrent duplicate submissions across the
    facilitator-verify gap collapse to a single serve. Expired rows are
    pruned opportunistically. An expired row can never mask a live
    authorization: the sanity check bounds the validity window to
    <= MAX_TIMEOUT_SECONDS <= the claim TTL, so a row only expires after
    its authorization is already invalid.
    """
    init_db()
    with sqlite3.connect(db_path()) as conn:
        try:
            conn.execute(
                "INSERT INTO x402_seen_signatures (signature, expires_at) VALUES (?, ?)",
                (signature, expires_at),
            )
        except sqlite3.IntegrityError:
            return True
        conn.execute(
            "DELETE FROM x402_seen_signatures WHERE expires_at <= ?",
            (time.time(),),
        )
        conn.commit()
        return False


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


def subscriber_monthly_credit_limit() -> float:
    """Resolved monthly fair-use credit cap: env override -> baked-in default."""
    raw = os.environ.get("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "").strip()
    if not raw:
        return DEFAULT_MONTHLY_CREDIT_LIMIT
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "Invalid SUBSCRIBER_MONTHLY_CREDIT_LIMIT=%r; using default %s",
            raw,
            DEFAULT_MONTHLY_CREDIT_LIMIT,
        )
        return DEFAULT_MONTHLY_CREDIT_LIMIT
    if not math.isfinite(value) or value < 0:
        log.warning(
            "Invalid SUBSCRIBER_MONTHLY_CREDIT_LIMIT=%r; using default %s",
            raw,
            DEFAULT_MONTHLY_CREDIT_LIMIT,
        )
        return DEFAULT_MONTHLY_CREDIT_LIMIT
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


def record_subscriber_usage(key: str, cost: float) -> dict:
    """Check + deduct fair-use credits for one subscriber-key-authenticated call.

    Tracks usage per subscriber-key hash (SHA-256, never the raw key) in the
    same SQLite DB; the counter rolls over on the calendar-month boundary
    (UTC). Calls whose credit cost would push usage over the cap are rejected
    and deduct NOTHING. Zero-cost calls (free/discovery paths) are always
    allowed, even at a zero credit limit.

    ``cost`` is the call's credit cost as computed by ``x402.price_to_credits``
    from the same per-tool price map the x402 middleware charges — billing
    never prices a call itself.

    Call only for keys already validated by ``valid_subscriber_key``.

    Returns ``{"allowed": bool, "credits_used": float, "credit_limit": float,
    "reset_at": str}``.

    Concurrency note: the read-modify-write is serialized by SQLite's
    single-writer lock, so exact cap enforcement under heavy concurrency is
    best-effort (a burst racing the boundary may admit one extra call).
    """
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    init_db()
    month = current_month()
    limit = subscriber_monthly_credit_limit()
    with sqlite3.connect(db_path()) as conn:
        row = conn.execute(
            "SELECT month, credits FROM subscriber_usage WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        used = float(row[1] or 0.0) if row is not None and row[0] == month else 0.0
        # round() kills binary floating-point drift from fractional credits
        # (e.g. 0.5/2.5 steps), so exact-boundary comparisons stay exact.
        new_used = round(used + cost, 6)
        if new_used <= limit:
            if row is None or row[0] != month:
                conn.execute(
                    "INSERT INTO subscriber_usage (key_hash, month, credits) "
                    "VALUES (?, ?, ?) ON CONFLICT(key_hash) DO UPDATE SET "
                    "month = excluded.month, credits = excluded.credits",
                    (key_hash, month, new_used),
                )
            else:
                conn.execute(
                    "UPDATE subscriber_usage SET credits = ? WHERE key_hash = ?",
                    (new_used, key_hash),
                )
            conn.commit()
            allowed, reported = True, new_used
        else:
            # Over the cap: reject without deducting anything.
            allowed, reported = False, used
    return {
        "allowed": allowed,
        "credits_used": reported,
        "credit_limit": limit,
        "reset_at": month_reset_at_iso(month),
    }


def get_subscriber_usage(key: str) -> dict:
    """Read-only usage peek for a subscriber key — never counts or 429s.

    Used by ``GET /billing/usage``; safe to call for any valid key, including
    keys that already hit the cap.
    """
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    init_db()
    month = current_month()
    path = db_path()
    used = 0.0
    if path.exists():
        try:
            with sqlite3.connect(path) as conn:
                row = conn.execute(
                    "SELECT month, credits FROM subscriber_usage WHERE key_hash = ?",
                    (key_hash,),
                ).fetchone()
        except sqlite3.Error:
            log.exception("subscriber usage DB read failed")
            row = None
        if row is not None and row[0] == month:
            used = float(row[1] or 0.0)
    return {
        "credits_used": used,
        "credit_limit": subscriber_monthly_credit_limit(),
        "resets_at": month_reset_at_iso(month),
        "period": month,
    }


# --------------------------------------------------------------------------
# Free tier (per client IP, calendar month UTC)


_FREE_TIER_SCHEMA = """CREATE TABLE IF NOT EXISTS free_tier_usage (
    ip_hash TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    credits REAL NOT NULL DEFAULT 0
)"""

# Monthly free credit bucket for anonymous callers. Business decision — this
# is Scott's call to tune. Default 50 credits = $0.50 of API calls at x402
# list prices (1 credit = $0.01). Same credit math as the subscriber cap:
# ``x402.price_to_credits`` is the single source of truth — billing never
# prices a call itself.
DEFAULT_FREE_MONTHLY_CREDITS = 50.0


def free_tier_enabled() -> bool:
    """Kill switch: ``FREE_TIER_ENABLED=false``/``0`` skips the free path
    entirely (calls fall straight to the x402 402 challenge)."""
    return _env_flag("FREE_TIER_ENABLED", "true")


def free_monthly_credit_limit() -> float:
    """Resolved monthly free credit bucket: env override -> baked-in default."""
    raw = os.environ.get("FREE_MONTHLY_CREDITS", "").strip()
    if not raw:
        return DEFAULT_FREE_MONTHLY_CREDITS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "Invalid FREE_MONTHLY_CREDITS=%r; using default %s",
            raw,
            DEFAULT_FREE_MONTHLY_CREDITS,
        )
        return DEFAULT_FREE_MONTHLY_CREDITS
    if not math.isfinite(value) or value < 0:
        log.warning(
            "Invalid FREE_MONTHLY_CREDITS=%r; using default %s",
            raw,
            DEFAULT_FREE_MONTHLY_CREDITS,
        )
        return DEFAULT_FREE_MONTHLY_CREDITS
    return value


def record_free_tier_usage(ip: str, cost: float) -> dict:
    """Check + deduct free-tier credits for one anonymous call.

    Keyed by SHA-256 of the client IP (never the raw IP), calendar month UTC.
    Calls whose credit cost would push usage over the bucket are rejected
    and deduct NOTHING. Zero-cost calls are always allowed, even at a zero
    credit limit.

    Returns ``{"allowed": bool, "credits_used": float, "free_limit": float,
    "reset_at": str}``.

    Same concurrency note as the subscriber cap: the read-modify-write is
    serialized by SQLite's single-writer lock, so exact cap enforcement under
    heavy concurrency is best-effort. Abuse backstop is the 60/min per-IP
    rate limit (slowapi), not this bucket — see PR body for the v1 IP-rotation
    limitation.
    """
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    init_db()
    month = current_month()
    limit = free_monthly_credit_limit()
    with sqlite3.connect(db_path()) as conn:
        row = conn.execute(
            "SELECT month, credits FROM free_tier_usage WHERE ip_hash = ?", (ip_hash,)
        ).fetchone()
        used = float(row[1] or 0.0) if row is not None and row[0] == month else 0.0
        new_used = round(used + cost, 6)
        if new_used <= limit:
            if row is None or row[0] != month:
                conn.execute(
                    "INSERT INTO free_tier_usage (ip_hash, month, credits) "
                    "VALUES (?, ?, ?) ON CONFLICT(ip_hash) DO UPDATE SET "
                    "month = excluded.month, credits = excluded.credits",
                    (ip_hash, month, new_used),
                )
            else:
                conn.execute(
                    "UPDATE free_tier_usage SET credits = ? WHERE ip_hash = ?",
                    (new_used, ip_hash),
                )
            conn.commit()
            allowed, reported = True, new_used
        else:
            allowed, reported = False, used
    return {
        "allowed": allowed,
        "credits_used": reported,
        "free_limit": limit,
        "reset_at": month_reset_at_iso(month),
    }


def get_free_tier_usage(ip: str) -> dict:
    """Read-only free-bucket peek for an IP — never deducts, never gates."""
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    init_db()
    month = current_month()
    used = 0.0
    if db_path().exists():
        try:
            with sqlite3.connect(db_path()) as conn:
                row = conn.execute(
                    "SELECT month, credits FROM free_tier_usage WHERE ip_hash = ?",
                    (ip_hash,),
                ).fetchone()
        except sqlite3.Error:
            log.exception("free tier DB read failed")
            row = None
        if row is not None and row[0] == month:
            used = float(row[1] or 0.0)
    return {
        "credits_used": used,
        "free_limit": free_monthly_credit_limit(),
        "resets_at": month_reset_at_iso(month),
        "period": month,
    }


# --------------------------------------------------------------------------
# Usage analytics (per-day rollups; no PII, IPs never stored)


_USAGE_DAILY_SCHEMA = """CREATE TABLE IF NOT EXISTS usage_daily (
    date TEXT NOT NULL,
    tool TEXT NOT NULL,
    plan_type TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, tool, plan_type)
)"""

PLAN_TYPES = ("free", "subscriber", "x402")

_MAX_STATS_DAYS = 365


def _utc_today() -> str:
    """Current date in UTC as ``YYYY-MM-DD``.

    Small and deliberately patchable: tests pin the date to simulate day
    boundaries without touching the clock.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def record_usage_rollup(tool: str, plan_type: str, credits: float) -> None:
    """Roll one served call into the ``usage_daily`` aggregates.

    ``credits`` is the credits actually deducted (free/subscriber) or the
    credits that WOULD have been charged (x402 path, from list price).
    Zero-credit outcomes are recorded as 0. Only aggregates are stored —
    no IPs, no keys, no payer info.
    """
    if plan_type not in PLAN_TYPES:
        raise ValueError(f"unknown plan_type {plan_type!r}")
    init_db()
    day = _utc_today()
    credits = round(float(credits or 0.0), 6)
    with sqlite3.connect(db_path()) as conn:
        conn.execute(
            "INSERT INTO usage_daily (date, tool, plan_type, calls, credits) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(date, tool, plan_type) DO UPDATE SET "
            "calls = usage_daily.calls + 1, "
            "credits = round(usage_daily.credits + excluded.credits, 6)",
            (day, tool, plan_type, credits),
        )
        conn.commit()


def get_usage_stats(days: int) -> dict:
    """Per-day usage aggregates plus totals for the last ``days`` days (UTC).

    ``days`` is clamped to 1..365. Shape::

        {"days": [{"date": "YYYY-MM-DD",
                   "tools": {name: {"free": {"calls", "credits"},
                                    "subscriber": {...}, "x402": {...}}}}],
         "totals": {"free": {"calls", "credits"},
                    "subscriber": {...}, "x402": {...}}}

    Every tool row carries all three plan keys (missing plans are 0s) so
    consumers get a stable shape. No PII: only aggregates.
    """
    days = max(1, min(_MAX_STATS_DAYS, days))
    init_db()
    # Pinned via _utc_today() so tests can simulate day boundaries.
    end = datetime.date.fromisoformat(_utc_today())
    start = end - datetime.timedelta(days=days - 1)
    per_day: dict[str, dict[str, dict[str, dict]]] = {}
    with sqlite3.connect(db_path()) as conn:
        rows = conn.execute(
            "SELECT date, tool, plan_type, calls, credits FROM usage_daily "
            "WHERE date >= ? AND date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    for date, tool, plan_type, calls, credits in rows:
        per_day.setdefault(date, {}).setdefault(tool, {})[plan_type] = {
            "calls": calls,
            "credits": credits,
        }
    days_out = []
    totals = {plan: {"calls": 0, "credits": 0.0} for plan in PLAN_TYPES}
    day = start
    while day <= end:
        ds = day.isoformat()
        tools = {}
        for tool, plans in per_day.get(ds, {}).items():
            entry = {plan: plans.get(plan, {"calls": 0, "credits": 0.0}) for plan in PLAN_TYPES}
            tools[tool] = entry
            for plan in PLAN_TYPES:
                totals[plan]["calls"] += entry[plan]["calls"]
                totals[plan]["credits"] = round(totals[plan]["credits"] + entry[plan]["credits"], 6)
        days_out.append({"date": ds, "tools": tools})
        day += datetime.timedelta(days=1)
    return {"days": days_out, "totals": totals}


def admin_key_valid(provided: str) -> bool:
    """True iff ``provided`` matches the ``ADMIN_KEY`` env shared secret.

    Fail closed: no ADMIN_KEY set -> nothing validates (the stats endpoint
    401s everything until Scott sets it on Render).
    """
    expected = os.environ.get("ADMIN_KEY", "").strip()
    if not expected:
        return False
    return secrets.compare_digest((provided or "").strip(), expected)


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
    # Stripe events are a few KB; refuse absurd bodies before buffering them.
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > 1024 * 1024:
        raise HTTPException(status_code=413, detail="payload too large")
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
        # M-2: only a subscription-mode checkout activates a subscriber. A
        # one-time payment (or a checkout for another product) must not
        # grant Pro credit — Stripe sends completed events for those too.
        if obj.get("mode") != "subscription":
            log.warning(
                "checkout.session.completed ignored: mode=%r (expected 'subscription')",
                obj.get("mode"),
            )
            return {"received": True}
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


def _session_is_paid_subscription(session: dict) -> bool:
    """M-2: a checkout session mints credit only when it is a PAID
    *subscription* checkout whose line items include the configured price.

    A one-time payment mode, an unpaid session, or a checkout built for a
    different product/price must never activate a subscriber — otherwise a
    Stripe test event or a misconfigured product would grant free Pro.
    """
    if session.get("mode") != "subscription":
        log.warning(
            "checkout session %s ignored: mode=%r (expected 'subscription')",
            session.get("id"),
            session.get("mode"),
        )
        return False
    if session.get("status") != "complete" or session.get("payment_status") != "paid":
        return False
    price_id = _stripe_price_id()
    items = (session.get("line_items") or {}).get("data") or []
    for item in items:
        price = item.get("price") or {}
        if price.get("id") == price_id:
            return True
    log.warning(
        "checkout session %s ignored: no line item for price %s",
        session.get("id"),
        price_id,
    )
    return False


@router.get("/billing/key")
def billing_key(session_id: str):
    """Exchange a completed checkout session for a subscriber token (shown once)."""
    _require_enabled()
    _stripe_api()
    try:
        session = stripe.checkout.Session.retrieve(
            session_id, expand=["customer", "line_items"]
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail="unknown checkout session") from exc
    session = _to_plain_dict(session)
    if not _session_is_paid_subscription(session):
        raise HTTPException(
            status_code=402,
            detail="checkout session is not a paid subscription for the configured price",
        )
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


@router.get("/billing/usage")
def billing_usage(request: Request):
    """Subscriber fair-use credit usage: ``{"credits_used", "credit_limit",
    "resets_at", "period"}``.

    Authed by ``X-Subscriber-Key`` (401 for missing/invalid keys). Read-only:
    it never deducts credits and is never 429-gated by the cap itself — the
    x402 middleware skips the fair-use check for this path, and the route
    only reads. FREE: ``/billing/*`` paths are never x402-gated.
    """
    _require_enabled()
    key = request.headers.get("x-subscriber-key", "").strip()
    if not key or not valid_subscriber_key(key):
        raise HTTPException(status_code=401, detail="invalid or missing X-Subscriber-Key")
    return get_subscriber_usage(key)


@router.get("/billing/stats")
def billing_stats(request: Request, days: int = 30, admin_key: str | None = None):
    """Usage analytics: per-day rollups + totals for the last ``days`` days.

    Authed by the ``ADMIN_KEY`` env shared secret, sent as the
    ``X-Admin-Key`` header or the ``?admin_key=`` query param; 401 JSON when
    missing/mismatched. Scott MUST set ``ADMIN_KEY`` on Render (long random
    string) — until then this endpoint 401s everything (fail closed).

    Cheap and un-metered: never consumes subscriber/free quota, never 402s
    (``/billing/*`` paths are never x402-gated and the middleware skips them
    before metering). No PII in the rollups — IPs are never stored, only
    date/tool/plan-type aggregates. ``days`` is clamped to 1..365.
    """
    provided = request.headers.get("x-admin-key", "") or (admin_key or "")
    if not admin_key_valid(provided):
        raise HTTPException(status_code=401, detail="invalid or missing admin key")
    return get_usage_stats(days)
