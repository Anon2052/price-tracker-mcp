"""Regression tests for the pre-launch security fixes (H-1..H-8, M-1..M-5).

Each test pins one audit finding so a future refactor can't silently
re-open it. H-1/H-2/H-3 live in tests/test_fetch.py.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from price_tracker import billing, x402
from price_tracker import rest_app as rest_app_mod
from price_tracker.rest_app import create_app

from .test_payments import (
    _enable_x402,
    _mock_facilitator,
    _x_payment_header,
)


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


# ------------------------------------------------------------------ M-1


def test_m1_oversize_validity_window_rejected(monkeypatch):
    """M-1: an authorization window wider than MAX_TIMEOUT_SECONDS (300s)
    is rejected at the sanity check, before any facilitator call."""
    _enable_x402(monkeypatch)
    _mock_facilitator(monkeypatch)
    now = int(time.time())
    xc = TestClient(create_app())
    r = xc.post(
        "/price",
        json={"url": "https://example.com/"},
        headers=_x_payment_header(valid_after=now - 60, valid_before=now + 300),
    )
    assert r.status_code == 402
    assert r.json()["reason"] == "authorization validity window exceeds maximum"


def test_m1_replay_claim_is_atomic_and_persistent(monkeypatch, tmp_path):
    """M-1: the replay claim is an atomic DB INSERT — the second claim of
    the same signature fails even across app instances (process restarts)."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "replay.db"))
    sig = "0x" + "cd" * 32
    assert billing.claim_x402_signature(sig, time.time() + 300) is False
    assert billing.claim_x402_signature(sig, time.time() + 300) is True
    import sqlite3

    rows = (
        sqlite3.connect(str(tmp_path / "replay.db"))
        .execute("SELECT COUNT(*) FROM x402_seen_signatures")
        .fetchone()
    )
    assert rows[0] == 1


# ------------------------------------------------------------------ M-5


def test_m5_bad_pay_to_fails_closed(monkeypatch):
    """M-5: a malformed X402_PAY_TO disables x402 instead of letting paid
    endpoints run unprotected."""
    _enable_x402(monkeypatch, pay_to="0xAbC123")
    assert x402.x402_config_problems() != []
    assert x402.x402_effective_enabled() is False


def test_m5_non_https_facilitator_fails_closed(monkeypatch):
    _enable_x402(monkeypatch, facilitator="http://x402.test/facilitator")
    assert x402.x402_config_problems() != []
    assert x402.x402_effective_enabled() is False


def test_m5_valid_config_stays_enabled(monkeypatch):
    _enable_x402(monkeypatch)
    assert x402.x402_config_problems() == []
    assert x402.x402_effective_enabled() is True


# ------------------------------------------------------------------ H-5


def test_h5_mcp_mount_is_rate_limited(monkeypatch):
    """H-5: the /mcp mount (invisible to SlowAPI's default_limits) is
    throttled by the pure-ASGI wrapper: over the per-minute budget -> 429."""
    _enable_x402(monkeypatch)
    xc = TestClient(create_app(rate_limit="3/minute"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with xc:
        statuses = [xc.post("/mcp", json=body).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    assert statuses[4] == 429


# ------------------------------------------------------------------ M-3


def _request(*, peer, xff=None, cf_ip=None):
    from types import SimpleNamespace

    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    if cf_ip is not None:
        headers["cf-connecting-ip"] = cf_ip
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_m3_spoofed_xff_ignored_on_direct_connection():
    """M-3: a direct client (global peer IP) cannot rotate the free-tier
    bucket by spoofing X-Forwarded-For."""
    assert x402.client_ip(_request(peer="8.8.8.8", xff="10.9.9.9")) == "8.8.8.8"


def test_m3_xff_honored_behind_proxy():
    assert x402.client_ip(_request(peer="10.0.0.5", xff="8.8.8.8, 10.0.0.5")) == "8.8.8.8"


def test_m3_garbage_xff_falls_back_to_peer():
    assert x402.client_ip(_request(peer="10.0.0.5", xff="not-an-ip")) == "10.0.0.5"


# --- Cloudflare: x402.client_ip (free-tier bucket) ---
# NOTE: the x402.client_ip Cloudflare-peer cases below were shadowed by the
# later rest_app._client_ip_from_scope definitions of the same names, so they
# never executed; the shadowed copies were removed (cleanup only) and the
# later definitions — the ones that actually ran — are kept as-is.


def test_cf_connecting_ip_spoof_ignored_from_non_cloudflare_peer():
    """A direct client cannot rotate the free-tier bucket by spoofing
    CF-Connecting-IP."""
    assert x402.client_ip(_request(peer="8.8.8.8", cf_ip="9.9.9.9")) == "8.8.8.8"


def test_cf_garbage_header_falls_back_to_peer():
    assert x402.client_ip(_request(peer="173.245.48.10", cf_ip="not-an-ip")) == "173.245.48.10"


def test_cf_peer_without_header_does_not_trust_xff():
    """Cloudflare appends to X-Forwarded-For, so its first entry is
    client-spoofable — with no CF-Connecting-IP we fall back to the peer,
    never XFF."""
    assert x402.client_ip(_request(peer="173.245.48.10", xff="9.9.9.9")) == "173.245.48.10"


# ------------------------------------------------- Cloudflare proxy flip
# (rate-limit key func: rest_app._client_ip_from_scope)


def _asgi_scope(*, peer, xff=None, cf_ip=None):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    if cf_ip is not None:
        headers.append((b"cf-connecting-ip", cf_ip.encode("latin-1")))
    return {"headers": headers, "client": (peer, 12345)}


def test_cf_connecting_ip_honored_from_cloudflare_peer():
    """CF-Connecting-IP is trusted when the TCP peer is a Cloudflare edge
    IP (proxied DNS). 172.64.1.10 is inside 172.64.0.0/13."""
    key = rest_app_mod._client_ip_from_scope(_asgi_scope(peer="172.64.1.10", cf_ip="8.8.8.8"))
    assert key == "8.8.8.8"


def test_cf_connecting_ip_honored_from_cloudflare_ipv6_peer():
    """Same fast path for a Cloudflare IPv6 edge peer (2606:4700::/32)."""
    key = rest_app_mod._client_ip_from_scope(
        _asgi_scope(peer="2606:4700:0:0:0:0:0:1", cf_ip="1.1.1.1")
    )
    assert key == "1.1.1.1"


def test_cf_connecting_ip_ignored_from_non_cloudflare_peer():
    """A spoofed CF-Connecting-IP from a direct (non-Cloudflare) peer is
    ignored — falls back to existing direct-connection logic (XFF ignored)."""
    key = rest_app_mod._client_ip_from_scope(_asgi_scope(peer="8.8.8.8", cf_ip="1.2.3.4"))
    assert key == "8.8.8.8"


def test_cf_connecting_ip_garbage_falls_back_to_peer():
    """Garbage CF-Connecting-IP from a Cloudflare peer falls back to the
    peer instead of poisoning the rate-limit bucket."""
    key = rest_app_mod._client_ip_from_scope(_asgi_scope(peer="172.64.1.10", cf_ip="not-an-ip"))
    assert key == "172.64.1.10"


def test_cf_peer_without_cf_header_ignores_xff():
    """Cloudflare peer with no CF-Connecting-IP: XFF is NOT trusted (first
    XFF entry is client-spoofable through Cloudflare) — falls back to the
    peer, same as any global peer."""
    key = rest_app_mod._client_ip_from_scope(_asgi_scope(peer="172.64.1.10", xff="8.8.8.8"))
    assert key == "172.64.1.10"


# ------------------------------------------------------------------ M-2


def test_m2_one_time_checkout_mode_rejected_at_key_exchange(monkeypatch, tmp_path):
    """M-2: a one-time (non-subscription) paid checkout must NOT mint a
    subscriber key, even when payment_status is paid."""
    import stripe as stripe_mod

    monkeypatch.setenv("STRIPE_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_test_123")
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))

    def fake_retrieve(session_id, expand=None):
        return {
            "id": session_id,
            "status": "complete",
            "mode": "payment",  # one-time, not subscription
            "payment_status": "paid",
            "customer": "cus_once",
            "customer_details": {"email": "o@e.z"},
            "line_items": {"data": [{"price": {"id": "price_test_123"}}]},
        }

    monkeypatch.setattr(stripe_mod.checkout.Session, "retrieve", fake_retrieve)
    r = TestClient(create_app()).get("/billing/key", params={"session_id": "cs_once"})
    assert r.status_code == 404
