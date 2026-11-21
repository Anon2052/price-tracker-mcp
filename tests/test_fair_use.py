"""Tests for the credit-based fair-use cap on the Pro subscriber subscription.


- 1 credit = $0.01 of x402 list price; cost per call = round(price/0.01, 1).
- The credit cost derives from the same per-tool price map the x402
  middleware charges from (``x402.tool_price_usdc``) — no duplicated table.
- Usage tracked per subscriber-key hash in the same SQLite DB (REAL credits,
  fractional); counter resets on calendar-month boundary (UTC);
  ``current_month`` is patchable so rollover is testable without touching
  the clock.
- Over the cap -> HTTP 429 with ``subscriber_monthly_cap_exceeded``,
  ``credits_used`` / ``credit_limit`` / ``resets_at`` fields, and the x402
  per-call fallback hint. The cap applies to subscriber-key auth only.
- ``GET /billing/usage`` is a free, read-only usage peek: never deducts,
  never 429-gated by the cap, never x402-gated.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from price_tracker import billing
from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app

from .test_payments import (
    _enable_x402,
    _mock_facilitator,
    _stub_price_backend,
    _x_payment_header,
)


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch
    DB so claims can never leak between tests (or touch the real path)."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "fair.db"))


def _enable_stripe(monkeypatch):
    monkeypatch.setenv("STRIPE_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_dummy")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_dummy")


# ------------------------------------------------- Build A default path


def test_db_default_path_is_disk_mount(monkeypatch):
    monkeypatch.delenv("BILLING_DB_PATH", raising=False)
    assert billing.db_path() == Path("/var/lib/price-tracker-mcp/billing.db")


def test_explicit_db_path_still_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "custom.db"))
    assert billing.db_path() == tmp_path / "custom.db"
    path = billing.init_db()
    assert path.exists()


def test_init_db_idempotent_on_fresh_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "newdir" / "nested" / "b.db"))
    first = billing.init_db()
    second = billing.init_db()
    assert first == second and first.exists()


def test_pre_credit_table_gets_credits_column(monkeypatch, tmp_path):
    """Tables created by the call-based build only have ``calls``; the credit
    rebuild must still work against them (column migration)."""
    _db(monkeypatch, tmp_path)
    path = billing.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE subscriber_usage "
            "(key_hash TEXT PRIMARY KEY, month TEXT NOT NULL, "
            "calls INTEGER NOT NULL DEFAULT 0)"
        )
        conn.commit()
    token = billing.create_subscriber("cus_mig", None)
    result = billing.record_subscriber_usage(token, 2.5)
    assert result["allowed"] and result["credits_used"] == 2.5
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriber_usage)")}
    assert "credits" in cols


# ------------------------------------------------- price -> credit conversion


def test_price_to_credits_spec_examples():
    assert x402_mod.price_to_credits(Decimal("0.025")) == 2.5
    assert x402_mod.price_to_credits(Decimal("0.01")) == 1.0
    assert x402_mod.price_to_credits(Decimal("0.005")) == 0.5


def test_credit_cost_uses_same_price_map_as_x402(monkeypatch):
    """No duplicated price table: the cost derives from tool_price_usdc."""
    assert x402_mod.tool_price_usdc("check_price") == Decimal("0.008")
    assert x402_mod.price_to_credits(x402_mod.tool_price_usdc("check_price")) == 0.8
    assert x402_mod.price_to_credits(x402_mod.tool_price_usdc("make_affiliate_link")) == 0.5
    # Env price overrides flow through automatically (single source of truth).
    monkeypatch.setenv("X402_PRICE_CHECK_PRICE_USDC", "0.025")
    assert x402_mod.price_to_credits(x402_mod.tool_price_usdc("check_price")) == 2.5


# ------------------------------------------------- limit config


def test_default_limit_is_1000_credits(monkeypatch):
    monkeypatch.delenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", raising=False)
    assert billing.subscriber_monthly_credit_limit() == 1000.0


def test_limit_env_override(monkeypatch):
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "50")
    assert billing.subscriber_monthly_credit_limit() == 50.0
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "2.5")
    assert billing.subscriber_monthly_credit_limit() == 2.5


def test_bad_limit_env_falls_back_to_default(monkeypatch):
    for bad in ("not-a-number", "-5", "", "nan", "inf", "-inf"):
        monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", bad)
        assert billing.subscriber_monthly_credit_limit() == 1000.0


def test_old_call_limit_env_is_gone(monkeypatch):
    """SUBSCRIBER_MONTHLY_CALL_LIMIT was removed: setting it must not change
    the credit limit."""
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "5")
    monkeypatch.delenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", raising=False)
    assert billing.subscriber_monthly_credit_limit() == 1000.0


def test_month_reset_at_iso():
    assert billing.month_reset_at_iso("2026-09") == "2026-10-01T00:00:00Z"
    assert billing.month_reset_at_iso("2026-12") == "2027-01-01T00:00:00Z"
    assert billing.month_reset_at_iso("2027-01") == "2027-02-01T00:00:00Z"


# ------------------------------------------------- usage accounting


def test_record_usage_deducts_fractional_credits(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    token = billing.create_subscriber("cus_u1", None)
    first = billing.record_subscriber_usage(token, 2.5)
    assert first == {
        "allowed": True,
        "credits_used": 2.5,
        "credit_limit": 1000.0,
        "reset_at": billing.month_reset_at_iso(billing.current_month()),
    }
    second = billing.record_subscriber_usage(token, 0.5)
    assert second["allowed"] and second["credits_used"] == 3.0


def test_month_rollover_resets_counter(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setattr(billing, "current_month", lambda: "2026-09")
    token = billing.create_subscriber("cus_u2", None)
    billing.record_subscriber_usage(token, 2.5)
    billing.record_subscriber_usage(token, 2.5)
    assert billing.record_subscriber_usage(token, 0.5)["credits_used"] == 5.5
    monkeypatch.setattr(billing, "current_month", lambda: "2026-10")
    rolled = billing.record_subscriber_usage(token, 2.5)
    assert rolled["allowed"] and rolled["credits_used"] == 2.5
    assert rolled["reset_at"] == "2026-11-01T00:00:00Z"


def test_over_cap_rejects_and_deducts_nothing(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "4")
    token = billing.create_subscriber("cus_u3", None)
    assert billing.record_subscriber_usage(token, 2.5)["allowed"]
    denied = billing.record_subscriber_usage(token, 2.5)
    assert denied == {
        "allowed": False,
        "credits_used": 2.5,
        "credit_limit": 4.0,
        "reset_at": billing.month_reset_at_iso(billing.current_month()),
    }
    # The rejected attempt deducted nothing: still 2.5, still denied.
    assert billing.record_subscriber_usage(token, 2.5)["credits_used"] == 2.5


def test_exact_boundary_is_allowed(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "5")
    token = billing.create_subscriber("cus_u4", None)
    billing.record_subscriber_usage(token, 2.5)
    exact = billing.record_subscriber_usage(token, 2.5)
    assert exact["allowed"] and exact["credits_used"] == 5.0
    assert billing.record_subscriber_usage(token, 0.5)["allowed"] is False


def test_zero_limit_blocks_paid_calls_only(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "0")
    token = billing.create_subscriber("cus_u5", None)
    result = billing.record_subscriber_usage(token, 2.5)
    assert result["allowed"] is False and result["credits_used"] == 0.0
    # Zero-cost (free/discovery) calls are always allowed.
    free = billing.record_subscriber_usage(token, 0.0)
    assert free["allowed"] and free["credits_used"] == 0.0


# ------------------------------------------------- HTTP behavior


@pytest.fixture()
def subscriber_client(monkeypatch, tmp_path):
    """App with x402 enabled, facilitator irrelevant (subscriber-key path)."""
    _enable_x402(monkeypatch)
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "4")
    _stub_price_backend(monkeypatch)
    x402_mod._reset_replay_cache()
    with TestClient(create_app()) as client:
        yield client


def test_subscriber_under_cap_served(subscriber_client):
    token = billing.create_subscriber("cus_http1", None)
    for _ in range(2):  # check_price $0.008 -> 0.8 credits each; 1.6 <= 4
        r = subscriber_client.post(
            "/price",
            json={"url": "https://example.com/"},
            headers={"X-Subscriber-Key": token},
        )
        assert r.status_code in (200, 422), r.text  # served, not 402/429


def test_subscriber_over_cap_429_with_credit_body(subscriber_client):
    token = billing.create_subscriber("cus_http2", None)
    headers = {"X-Subscriber-Key": token}
    # 5 calls x 0.8 credits = 4.0: exactly the cap.
    for _ in range(5):
        r = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
        assert r.status_code in (200, 422), r.text
    r = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["error"] == "subscriber_monthly_cap_exceeded"
    assert body["credits_used"] == 4.0
    assert body["credit_limit"] == 4.0
    # Reset date: first of next month, UTC, ISO Zulu.
    assert body["resets_at"] == billing.month_reset_at_iso(billing.current_month())
    assert body["resets_at"].endswith("-01T00:00:00Z")
    # User-actionable: the per-call x402 fallback is spelled out.
    assert "X-Payment" in body["message"]
    assert "without X-Subscriber-Key" in body["message"]
    # Rejected attempts deduct nothing: still 4.0, still 429.
    r2 = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r2.status_code == 429
    assert r2.json()["credits_used"] == 4.0


def test_overridden_price_deducts_fractional_credits_over_http(monkeypatch, tmp_path):
    """A $0.025 list price deducts exactly 2.5 credits per call over HTTP."""
    _enable_x402(monkeypatch, prices={"X402_PRICE_CHECK_PRICE_USDC": "0.025"})
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "5")
    _stub_price_backend(monkeypatch)
    with TestClient(create_app()) as client:
        token = billing.create_subscriber("cus_http5", None)
        headers = {"X-Subscriber-Key": token}
        r = client.post("/price", json={"url": "https://example.com/"}, headers=headers)
        assert r.status_code in (200, 422), r.text
        usage = billing.get_subscriber_usage(token)
        assert usage["credits_used"] == 2.5
        assert usage["credit_limit"] == 5.0


def test_month_rollover_resets_cap_over_http(subscriber_client, monkeypatch):
    token = billing.create_subscriber("cus_http3", None)
    headers = {"X-Subscriber-Key": token}
    for _ in range(5):  # 5 x 0.8 = 4.0: exactly the cap
        subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert (
        subscriber_client.post(
            "/price", json={"url": "https://example.com/"}, headers=headers
        ).status_code
        == 429
    ), "expected the cap to be hit on the 6th call"
    # New calendar month (UTC): the same key is served again. Computed from
    # the real month so the test can't go stale when the calendar turns.
    real = billing.current_month()
    year, mon = int(real[:4]), int(real[5:7])
    nxt = f"{year + (mon == 12):04d}-{mon % 12 + 1:02d}"
    monkeypatch.setattr(billing, "current_month", lambda: nxt)
    r = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code in (200, 422), r.text  # served again after rollover


def test_cap_covers_mcp_tools_call(subscriber_client):
    """The cap hooks the X-Subscriber-Key validation point, so MCP calls count."""
    token = billing.create_subscriber("cus_http4", None)
    headers = {"X-Subscriber-Key": token}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "check_price", "arguments": {"url": "https://example.com/"}},
    }
    for _ in range(5):  # 5 x 0.8 = 4.0: exactly the cap
        r = subscriber_client.post("/mcp", json=body, headers=headers)
        assert r.status_code != 429  # served or tool-level error, not capped
    sixth = subscriber_client.post("/mcp", json=body, headers=headers)
    assert sixth.status_code == 429
    assert sixth.json()["error"] == "subscriber_monthly_cap_exceeded"
    assert sixth.json()["credits_used"] == 4.0


def test_x402_payers_not_capped(monkeypatch, tmp_path):
    """Per-call x402 payers are outside the cap: they pay, they play."""
    _enable_x402(monkeypatch)
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "1")
    x402_mod._reset_replay_cache()
    _mock_facilitator(monkeypatch)
    _stub_price_backend(monkeypatch)
    with TestClient(create_app()) as client:
        for i in range(3):
            r = client.post(
                "/price",
                json={"url": "https://example.com/"},
                headers=_x_payment_header(signature=f"0xNoCap{i}"),
            )
            assert r.status_code in (200, 422), r.text
            assert r.status_code != 429


# ------------------------------------------------- GET /billing/usage


@pytest.fixture()
def usage_client(monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch)
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CREDIT_LIMIT", "4")
    _stub_price_backend(monkeypatch)
    x402_mod._reset_replay_cache()
    with TestClient(create_app()) as client:
        yield client


def test_billing_usage_returns_accurate_numbers(usage_client):
    token = billing.create_subscriber("cus_usage1", None)
    headers = {"X-Subscriber-Key": token}
    r = usage_client.get("/billing/usage", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "credits_used": 0.0,
        "credit_limit": 4.0,
        "resets_at": billing.month_reset_at_iso(billing.current_month()),
        "period": billing.current_month(),
    }
    usage_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    body = usage_client.get("/billing/usage", headers=headers).json()
    assert body["credits_used"] == 0.8
    assert body["credit_limit"] == 4.0


def test_billing_usage_never_counts_or_429s(usage_client):
    """The usage peek itself is free: it doesn't deduct, and it works even
    when the key already hit the cap."""
    token = billing.create_subscriber("cus_usage2", None)
    headers = {"X-Subscriber-Key": token}
    for _ in range(5):  # 5 x 0.8 = 4.0: exactly the cap
        usage_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    r = usage_client.get("/billing/usage", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credits_used"] == 4.0
    # The peek deducted nothing: usage is unchanged afterwards.
    assert billing.get_subscriber_usage(token)["credits_used"] == 4.0


def test_billing_usage_rejects_bad_key(usage_client):
    r = usage_client.get("/billing/usage")
    assert r.status_code == 401
    r = usage_client.get("/billing/usage", headers={"X-Subscriber-Key": "bogus"})
    assert r.status_code == 401
