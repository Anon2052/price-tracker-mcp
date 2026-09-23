"""Tests for the monthly free credit tier and the usage-analytics rollups.

Free tier (workstream A):
- Monthly free bucket ``FREE_MONTHLY_CREDITS`` (default 50), keyed by client
  IP from ``X-Forwarded-For`` (first entry). No key, no payment required.
- Same credit math as subscribers: ``round(price_usd/0.01, 1)`` from the
  single x402 price map (no duplicated table).
- Middleware order: valid X-Subscriber-Key -> subscriber path (429 code
  untouched) -> X-Payment present -> x402 flow (exempt from the bucket) ->
  free bucket -> 402. REST + MCP share the hook.
- Exhausted bucket -> 402 ``free_tier_exhausted`` (NOT 429), with
  credits_used / free_limit / resets_at / upgrade_hint.
- Denied/exhausted calls never consume credits. ``FREE_TIER_ENABLED=0``
  skips the free path entirely (straight to the x402 402).

Usage analytics (workstream B):
- ``usage_daily`` rollups (date/tool/plan_type) in the same SQLite DB.
- Every served call recorded: free/subscriber record deducted credits,
  x402 records list-price credits, zero-credit outcomes recorded as 0.
- ``GET /billing/stats?days=30``: per-day aggregates + totals, gated by
  ``ADMIN_KEY`` (``X-Admin-Key`` header or ``?admin_key=``), 401 otherwise.
  Cheap and un-metered. No PII — IPs never stored, only aggregates.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from price_tracker import billing
from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app

from .test_payments import (
    _mock_facilitator,
    _stub_price_backend,
    _x_payment_header,
)

PAY_TO = "0xAbC1230000000000000000000000000000000000"
IP_A = "10.0.0.1"
IP_B = "10.0.0.2"


def _free_headers(ip: str = IP_A) -> dict:
    return {"X-Forwarded-For": ip}


@pytest.fixture()
def free_client(monkeypatch, tmp_path):
    """App with x402 enabled and the free tier ON (default), small bucket."""
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.delenv("FREE_TIER_ENABLED", raising=False)  # default: on
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "4")
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "free.db"))
    monkeypatch.setenv("ADMIN_KEY", "test-admin-secret-123")
    _stub_price_backend(monkeypatch)
    x402_mod._reset_replay_cache()
    with TestClient(create_app()) as client:
        yield client


# ------------------------------------------------- free tier: deduction


def test_free_bucket_deducts_per_price(free_client):
    for _ in range(2):  # check_price $0.02 -> 2.0 credits each
        r = free_client.post(
            "/price", json={"url": "https://example.com/"}, headers=_free_headers()
        )
        assert r.status_code in (200, 422), r.text  # served, not 402/429
    usage = billing.get_free_tier_usage(IP_A)
    assert usage["credits_used"] == 4.0
    assert usage["free_limit"] == 4.0
    assert usage["period"] == billing.current_month()


def test_free_bucket_uses_same_price_map_as_x402(monkeypatch):
    """No duplicated price table: the deduction derives from tool_price_usdc."""
    assert x402_mod.price_to_credits(x402_mod.tool_price_usdc("check_price")) == 2.0
    assert x402_mod.price_to_credits(x402_mod.tool_price_usdc("make_affiliate_link")) == 0.5


def test_default_free_limit_is_50_credits(monkeypatch):
    monkeypatch.delenv("FREE_MONTHLY_CREDITS", raising=False)
    assert billing.free_monthly_credit_limit() == 50.0


def test_bad_free_limit_env_falls_back_to_default(monkeypatch):
    for bad in ("not-a-number", "-5", "", "nan", "inf"):
        monkeypatch.setenv("FREE_MONTHLY_CREDITS", bad)
        assert billing.free_monthly_credit_limit() == 50.0


def test_free_tier_enabled_default_and_kill_switch(monkeypatch):
    monkeypatch.delenv("FREE_TIER_ENABLED", raising=False)
    assert billing.free_tier_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("FREE_TIER_ENABLED", off)
        assert billing.free_tier_enabled() is False
    for on in ("1", "true", "yes"):
        monkeypatch.setenv("FREE_TIER_ENABLED", on)
        assert billing.free_tier_enabled() is True


def test_affiliate_deducts_fractional_credits(free_client):
    for _ in range(2):  # make_affiliate_link $0.005 -> 0.5 credits each
        r = free_client.post(
            "/affiliate",
            json={"url": "https://www.amazon.com/dp/B0X", "tag": "t-20"},
            headers=_free_headers(),
        )
        assert r.status_code == 200, r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 1.0


# ------------------------------------------------- free tier: exhaustion


def test_exhaustion_returns_402_with_fields(free_client):
    headers = _free_headers()
    for _ in range(2):
        free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    r = free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["error"] == "free_tier_exhausted"  # 402, NOT 429
    assert body["credits_used"] == 4.0
    assert body["free_limit"] == 4.0
    assert body["resets_at"] == billing.month_reset_at_iso(billing.current_month())
    assert body["resets_at"].endswith("-01T00:00:00Z")
    # Actionable: the x402 challenge is embedded and the upgrade path spelled out.
    assert "paymentRequirements" in body
    assert "upgrade_hint" in body
    assert "x402" in body["upgrade_hint"]
    assert "$9/mo" in body["upgrade_hint"]


def test_exhausted_calls_never_consume_credits(free_client):
    headers = _free_headers()
    for _ in range(2):
        free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    for _ in range(3):
        r = free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
        assert r.status_code == 402
        assert r.json()["error"] == "free_tier_exhausted"
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 4.0


def test_month_reset_refills_bucket(free_client, monkeypatch):
    headers = _free_headers()
    for _ in range(2):
        free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert (
        free_client.post(
            "/price", json={"url": "https://example.com/"}, headers=headers
        ).status_code
        == 402
    )
    # New calendar month (UTC): the same IP is served again.
    monkeypatch.setattr(billing, "current_month", lambda: "2026-10")
    r = free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code in (200, 422), r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 2.0
    assert billing.get_free_tier_usage(IP_A)["period"] == "2026-10"


# ------------------------------------------------- free tier: IP keying


def test_separate_buckets_per_ip(free_client):
    for _ in range(2):
        free_client.post(
            "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
        )
    r = free_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    assert r.status_code == 402
    # A different IP still has a full bucket.
    r = free_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_B)
    )
    assert r.status_code in (200, 422), r.text
    assert billing.get_free_tier_usage(IP_B)["credits_used"] == 2.0


def test_x_forwarded_for_first_entry_wins(free_client):
    for _ in range(2):
        free_client.post(
            "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
        )
    # "IP_A, proxy" must key on IP_A (exhausted) -> 402, not on the proxy.
    r = free_client.post(
        "/price",
        json={"url": "https://example.com/"},
        headers={"X-Forwarded-For": f"{IP_A}, 203.0.113.9"},
    )
    assert r.status_code == 402
    assert r.json()["error"] == "free_tier_exhausted"


def test_free_bucket_stores_hash_not_raw_ip(free_client, tmp_path):
    free_client.post("/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A))
    with sqlite3.connect(billing.db_path()) as conn:
        rows = conn.execute("SELECT ip_hash FROM free_tier_usage").fetchall()
    assert len(rows) == 1
    assert IP_A not in rows[0][0]
    assert len(rows[0][0]) == 64  # sha256 hex


# ------------------------------------------------- free tier: kill switch + discovery


def test_free_tier_disabled_falls_straight_to_402(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.setenv("FREE_TIER_ENABLED", "0")
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "nokill.db"))
    _stub_price_backend(monkeypatch)
    with TestClient(create_app()) as client:
        r = client.post("/price", json={"url": "https://example.com/"}, headers=_free_headers())
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required"  # the plain x402 challenge
    assert "upgrade_hint" in body  # directs to x402 or subscribe
    # The free path was skipped entirely: no bucket row, no deduction.
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 0.0


def test_discovery_never_deducts(free_client):
    r = free_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_free_headers(),
    )
    assert r.status_code == 200, r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 0.0


def test_mcp_tools_call_shares_bucket(free_client):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "check_price", "arguments": {"url": "https://example.com/"}},
    }
    r = free_client.post("/mcp", json=body, headers=_free_headers())
    assert r.status_code == 200, r.text  # served from the free bucket
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 2.0
    # Same bucket as REST: one more REST call spends it.
    free_client.post("/price", json={"url": "https://example.com/"}, headers=_free_headers())
    r = free_client.post("/price", json={"url": "https://example.com/"}, headers=_free_headers())
    assert r.status_code == 402
    assert r.json()["error"] == "free_tier_exhausted"


# ------------------------------------------------- path precedence


def test_subscriber_path_unaffected(free_client, monkeypatch):
    """Valid subscriber key -> subscriber cap path; the free bucket is
    untouched and its own 429 code is unchanged."""
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "4")
    token = billing.create_subscriber("cus_free1", None)
    headers = {"X-Subscriber-Key": token, "X-Forwarded-For": IP_A}
    for _ in range(2):
        r = free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
        assert r.status_code in (200, 422), r.text
    # The free bucket was NOT touched by subscriber-keyed calls.
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 0.0
    # Subscriber cap still 429s with its own code (never 402 free_tier_exhausted).
    r = free_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code == 429
    assert r.json()["error"] == "subscriber_monthly_cap_exceeded"
    # Same IP without a key still draws from the free bucket (served, not 429).
    r = free_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    assert r.status_code in (200, 422), r.text


def test_x402_paid_path_unaffected(free_client, monkeypatch):
    """A verified x402 payment bypasses the free bucket entirely."""
    _mock_facilitator(monkeypatch)
    for i in range(3):
        r = free_client.post(
            "/price",
            json={"url": "https://example.com/"},
            headers={**_free_headers(), **_x_payment_header(signature=f"0xFree{i}")},
        )
        assert r.status_code in (200, 422), r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 0.0


def test_invalid_subscriber_key_falls_through_to_free(free_client):
    """A bogus subscriber key is not an error: it falls through to the free
    bucket (existing x402 fall-through behavior, free tier inserted)."""
    r = free_client.post(
        "/price",
        json={"url": "https://example.com/"},
        headers={"X-Subscriber-Key": "bogus", "X-Forwarded-For": IP_A},
    )
    assert r.status_code in (200, 422), r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 2.0


# ------------------------------------------------- analytics: rollups


@pytest.fixture()
def analytics_client(monkeypatch, tmp_path):
    """App with x402 enabled, free tier ON, facilitator mocked."""
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.delenv("FREE_TIER_ENABLED", raising=False)
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "100")
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "stats.db"))
    monkeypatch.setenv("ADMIN_KEY", "test-admin-secret-123")
    _stub_price_backend(monkeypatch)
    _mock_facilitator(monkeypatch)
    x402_mod._reset_replay_cache()
    with TestClient(create_app()) as client:
        yield client


def _admin_headers():
    return {"X-Admin-Key": "test-admin-secret-123"}


def test_rollup_increments_across_plan_types(analytics_client):
    # free call: $0.02 -> 2.0 credits deducted from the bucket
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    # subscriber call: 2.0 credits deducted from the key
    token = billing.create_subscriber("cus_stats1", None)
    analytics_client.post(
        "/price",
        json={"url": "https://example.com/"},
        headers={"X-Subscriber-Key": token, "X-Forwarded-For": IP_A},
    )
    # x402 call: 2.0 credits that WOULD have been charged (list price)
    analytics_client.post(
        "/price",
        json={"url": "https://example.com/"},
        headers={**_free_headers(IP_A), **_x_payment_header(signature="0xStats1")},
    )
    stats = billing.get_usage_stats(1)
    tools = stats["days"][0]["tools"]["check_price"]
    assert tools["free"] == {"calls": 1, "credits": 2.0}
    assert tools["subscriber"] == {"calls": 1, "credits": 2.0}
    assert tools["x402"] == {"calls": 1, "credits": 2.0}
    totals = stats["totals"]
    assert totals["free"] == {"calls": 1, "credits": 2.0}
    assert totals["subscriber"] == {"calls": 1, "credits": 2.0}
    assert totals["x402"] == {"calls": 1, "credits": 2.0}


def test_multi_tool_separation(analytics_client):
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    analytics_client.post(
        "/affiliate",
        json={"url": "https://www.amazon.com/dp/B0X", "tag": "t-20"},
        headers=_free_headers(IP_A),
    )
    stats = billing.get_usage_stats(1)
    tools = stats["days"][0]["tools"]
    assert set(tools) == {"check_price", "make_affiliate_link"}
    assert tools["check_price"]["free"] == {"calls": 1, "credits": 2.0}
    assert tools["make_affiliate_link"]["free"] == {"calls": 1, "credits": 0.5}


def test_zero_credit_outcomes_recorded_as_zero(analytics_client):
    analytics_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=_free_headers(IP_A),
    )
    stats = billing.get_usage_stats(1)
    assert stats["days"][0]["tools"]["mcp_discovery"]["free"] == {"calls": 1, "credits": 0}


def test_day_boundaries_utc(analytics_client, monkeypatch):
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    monkeypatch.setattr(billing, "_utc_today", lambda: "2026-09-24")
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    stats = billing.get_usage_stats(7)
    by_date = {d["date"]: d["tools"] for d in stats["days"]}
    assert "2026-09-24" in by_date
    assert by_date["2026-09-24"]["check_price"]["free"] == {"calls": 1, "credits": 2.0}


def test_denied_calls_not_recorded(analytics_client, monkeypatch):
    """429s (subscriber cap) and exhausted-bucket 402s are not served calls."""
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "2")
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2")
    token = billing.create_subscriber("cus_stats2", None)
    sub_headers = {"X-Subscriber-Key": token, "X-Forwarded-For": IP_A}
    # Spend both buckets once, then hit both denials.
    analytics_client.post("/price", json={"url": "https://example.com/"}, headers=sub_headers)
    assert (
        analytics_client.post(
            "/price", json={"url": "https://example.com/"}, headers=sub_headers
        ).status_code
        == 429
    )
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_B)
    )
    assert (
        analytics_client.post(
            "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_B)
        ).status_code
        == 402
    )
    stats = billing.get_usage_stats(1)
    tools = stats["days"][0]["tools"]["check_price"]
    assert tools["subscriber"] == {"calls": 1, "credits": 2.0}
    assert tools["free"] == {"calls": 1, "credits": 2.0}


def test_no_pii_in_rollups(analytics_client):
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    stats = billing.get_usage_stats(30)
    assert IP_A not in json.dumps(stats)
    with sqlite3.connect(billing.db_path()) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(usage_daily)")}
    assert cols == {"date", "tool", "plan_type", "calls", "credits"}


# ------------------------------------------------- analytics: /billing/stats


def test_stats_endpoint_shape(analytics_client):
    analytics_client.post(
        "/price", json={"url": "https://example.com/"}, headers=_free_headers(IP_A)
    )
    r = analytics_client.get("/billing/stats", headers=_admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["days"]) == 30  # default days=30
    for day in body["days"]:
        assert set(day) == {"date", "tools"}
    today = body["days"][-1]
    assert set(today["tools"]["check_price"]) == {"free", "subscriber", "x402"}
    assert today["tools"]["check_price"]["free"] == {"calls": 1, "credits": 2.0}
    assert set(body["totals"]) == {"free", "subscriber", "x402"}
    assert body["totals"]["free"] == {"calls": 1, "credits": 2.0}


def test_stats_auth_header_and_query_param(analytics_client):
    assert analytics_client.get("/billing/stats", headers=_admin_headers()).status_code == 200
    r = analytics_client.get("/billing/stats", params={"admin_key": "test-admin-secret-123"})
    assert r.status_code == 200, r.text


def test_stats_401_without_or_with_wrong_key(analytics_client):
    r = analytics_client.get("/billing/stats")
    assert r.status_code == 401
    assert r.json()["detail"]
    r = analytics_client.get("/billing/stats", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401
    r = analytics_client.get("/billing/stats", params={"admin_key": "wrong"})
    assert r.status_code == 401


def test_stats_401_when_admin_key_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.test/facilitator")
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "nokey.db"))
    _stub_price_backend(monkeypatch)
    with TestClient(create_app()) as client:
        r = client.get("/billing/stats", headers={"X-Admin-Key": "anything"})
    assert r.status_code == 401  # fail closed until Scott sets ADMIN_KEY


def test_stats_days_clamping(analytics_client):
    r = analytics_client.get("/billing/stats", params={"days": 0}, headers=_admin_headers())
    assert r.status_code == 200
    assert len(r.json()["days"]) == 1
    r = analytics_client.get("/billing/stats", params={"days": -5}, headers=_admin_headers())
    assert len(r.json()["days"]) == 1
    r = analytics_client.get("/billing/stats", params={"days": 7}, headers=_admin_headers())
    assert len(r.json()["days"]) == 7
    r = analytics_client.get("/billing/stats", params={"days": 100000}, headers=_admin_headers())
    assert len(r.json()["days"]) == 365


def test_stats_is_unmetered(analytics_client, monkeypatch):
    """The stats endpoint never consumes quota and never 402s."""
    monkeypatch.setenv("FREE_MONTHLY_CREDITS", "2")
    for _ in range(5):
        r = analytics_client.get("/billing/stats", headers=_admin_headers())
        assert r.status_code == 200, r.text
    assert billing.get_free_tier_usage(IP_A)["credits_used"] == 0.0
    token = billing.create_subscriber("cus_stats3", None)
    r = analytics_client.get(
        "/billing/stats",
        headers={"X-Admin-Key": "test-admin-secret-123", "X-Subscriber-Key": token},
    )
    assert r.status_code == 200
    assert billing.get_subscriber_usage(token)["credits_used"] == 0.0


def test_record_usage_rollup_rejects_unknown_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "bad.db"))
    with pytest.raises(ValueError):
        billing.record_usage_rollup("check_price", "platinum", 1.0)
