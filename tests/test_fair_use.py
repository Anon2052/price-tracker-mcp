"""Tests for Build B: fair-use monthly cap on the Pro subscriber subscription.

- Usage tracked per subscriber-key hash in the same SQLite DB.
- Counter resets on calendar-month boundary (UTC); ``current_month`` is
  patchable so rollover is testable without touching the clock.
- Over the cap -> HTTP 429 with an actionable body (cap, usage, reset date,
  x402 per-call fallback). The cap applies to subscriber-key auth only.
"""

from __future__ import annotations

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


def _db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "fair.db"))


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


# ------------------------------------------------- limit config


def test_default_limit_is_1000(monkeypatch):
    monkeypatch.delenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", raising=False)
    assert billing.subscriber_monthly_call_limit() == 1000


def test_limit_env_override(monkeypatch):
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "50")
    assert billing.subscriber_monthly_call_limit() == 50


def test_bad_limit_env_falls_back_to_default(monkeypatch):
    for bad in ("not-a-number", "-5", ""):
        monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", bad)
        assert billing.subscriber_monthly_call_limit() == 1000


def test_month_reset_at_iso():
    assert billing.month_reset_at_iso("2026-09") == "2026-10-01T00:00:00Z"
    assert billing.month_reset_at_iso("2026-12") == "2027-01-01T00:00:00Z"
    assert billing.month_reset_at_iso("2027-01") == "2027-02-01T00:00:00Z"


# ------------------------------------------------- usage accounting


def test_record_usage_increments(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    token = billing.create_subscriber("cus_u1", None)
    first = billing.record_subscriber_call(token)
    assert first == {
        "allowed": True,
        "usage": 1,
        "limit": 1000,
        "reset_at": billing.month_reset_at_iso(billing.current_month()),
    }
    second = billing.record_subscriber_call(token)
    assert second["allowed"] and second["usage"] == 2


def test_month_rollover_resets_counter(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setattr(billing, "current_month", lambda: "2026-09")
    token = billing.create_subscriber("cus_u2", None)
    billing.record_subscriber_call(token)
    billing.record_subscriber_call(token)
    assert billing.record_subscriber_call(token)["usage"] == 3
    monkeypatch.setattr(billing, "current_month", lambda: "2026-10")
    rolled = billing.record_subscriber_call(token)
    assert rolled["allowed"] and rolled["usage"] == 1
    assert rolled["reset_at"] == "2026-11-01T00:00:00Z"


def test_over_cap_rejects_and_does_not_count(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "2")
    token = billing.create_subscriber("cus_u3", None)
    assert billing.record_subscriber_call(token)["allowed"]
    assert billing.record_subscriber_call(token)["allowed"]
    denied = billing.record_subscriber_call(token)
    assert denied == {
        "allowed": False,
        "usage": 2,
        "limit": 2,
        "reset_at": billing.month_reset_at_iso(billing.current_month()),
    }
    # The rejected attempt was not counted: still 2, still denied.
    assert billing.record_subscriber_call(token)["usage"] == 2


def test_zero_limit_blocks_first_call(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "0")
    token = billing.create_subscriber("cus_u4", None)
    result = billing.record_subscriber_call(token)
    assert result["allowed"] is False and result["usage"] == 0


# ------------------------------------------------- HTTP behavior


@pytest.fixture()
def subscriber_client(monkeypatch, tmp_path):
    """App with x402 enabled, facilitator irrelevant (subscriber-key path)."""
    _enable_x402(monkeypatch)
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "2")
    _stub_price_backend(monkeypatch)
    x402_mod._reset_replay_cache()
    with TestClient(create_app()) as client:
        yield client


def test_subscriber_under_cap_served(subscriber_client):
    token = billing.create_subscriber("cus_http1", None)
    for _ in range(2):
        r = subscriber_client.post(
            "/price",
            json={"url": "https://example.com/"},
            headers={"X-Subscriber-Key": token},
        )
        assert r.status_code in (200, 422), r.text  # served, not 402/429


def test_subscriber_over_cap_429_with_actionable_body(subscriber_client):
    token = billing.create_subscriber("cus_http2", None)
    headers = {"X-Subscriber-Key": token}
    for _ in range(2):
        r = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
        assert r.status_code in (200, 422), r.text
    r = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["error"] == "subscriber_monthly_limit_exceeded"
    assert body["monthly_call_limit"] == 2
    assert body["calls_used"] == 2
    # Reset date: first of next month, UTC, ISO Zulu.
    assert body["resets_at"] == billing.month_reset_at_iso(billing.current_month())
    assert body["resets_at"].endswith("-01T00:00:00Z")
    # User-actionable: the per-call x402 fallback is spelled out.
    assert "x402" in body["message"]
    assert "X-Subscriber-Key" in body["message"]
    # Rejected attempts don't consume quota: still 2, still 429.
    r2 = subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r2.status_code == 429
    assert r2.json()["calls_used"] == 2


def test_month_rollover_resets_cap_over_http(subscriber_client, monkeypatch):
    token = billing.create_subscriber("cus_http3", None)
    headers = {"X-Subscriber-Key": token}
    for _ in range(2):
        subscriber_client.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert (
        subscriber_client.post(
            "/price", json={"url": "https://example.com/"}, headers=headers
        ).status_code
        == 429
    ), "expected the cap to be hit on the 3rd call"
    # New calendar month (UTC): the same key is served again.
    monkeypatch.setattr(billing, "current_month", lambda: "2026-10")
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
    first = subscriber_client.post("/mcp", json=body, headers=headers)
    assert first.status_code != 429  # served or tool-level error, not capped
    second = subscriber_client.post("/mcp", json=body, headers=headers)
    assert second.status_code != 429
    third = subscriber_client.post("/mcp", json=body, headers=headers)
    assert third.status_code == 429
    assert third.json()["error"] == "subscriber_monthly_limit_exceeded"


def test_x402_payers_not_capped(monkeypatch, tmp_path):
    """Per-call x402 payers are outside the cap: they pay, they play."""
    _enable_x402(monkeypatch)
    _db(monkeypatch, tmp_path)
    monkeypatch.setenv("SUBSCRIBER_MONTHLY_CALL_LIMIT", "1")
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
