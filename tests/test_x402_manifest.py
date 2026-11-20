"""Tests for the x402 discovery manifest at /.well-known/x402.json.

Honesty is the core contract: x402 is implemented but DISABLED in
production until the LLC/Stripe go-live, so the manifest must NEVER claim
payments are live. All prices are derived from the single source of truth
(price_tracker.x402.TOOL_PRICE_DEFAULTS via tool_price_usdc()) — there is
no duplicated price table, so these tests assert equality against the map
and against env overrides.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


def _manifest(resp) -> dict:
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text[:200]}"
    assert "application/json" in resp.headers["content-type"]
    return resp.json()


def test_manifest_reachable_without_auth_key_or_payment(client):
    # Plain GET, no X-Subscriber-Key, no X-Payment: must be 200, never 402.
    r = client.get("/.well-known/x402.json")
    body = _manifest(r)
    assert body  # valid, non-empty JSON


def test_manifest_prices_match_single_source_of_truth(client):
    # Every price in the manifest must equal the resolved price from the
    # module's single price map — not a hardcoded copy.
    body = _manifest(client.get("/.well-known/x402.json"))
    assert set(body["tools"]) == set(x402_mod.TOOL_PRICE_DEFAULTS)
    for tool in x402_mod.TOOL_PRICE_DEFAULTS:
        assert body["tools"][tool]["price_usdc"] == str(x402_mod.tool_price_usdc(tool))


def test_manifest_prices_follow_env_overrides(client, monkeypatch):
    # Env price overrides (X402_PRICE_<NAME>_USDC) must flow through the
    # manifest: proof it derives from the map instead of hardcoding.
    monkeypatch.setenv("X402_PRICE_CHECK_PRICE_USDC", "0.05")
    body = _manifest(client.get("/.well-known/x402.json"))
    assert body["tools"]["check_price"]["price_usdc"] == "0.05"


def test_manifest_status_never_claims_live_payments(client):
    body = _manifest(client.get("/.well-known/x402.json"))
    status = body["x402_status"]
    assert status == "implemented_but_disabled"
    # The live-state status value must never leak into a disabled manifest.
    assert "accepting_payments" not in json.dumps(body)
    assert "disabled" in status
    assert "disabled" in body["status_note"].lower()


def test_manifest_status_disabled_when_flag_on_but_config_incomplete(client, monkeypatch):
    # Fail closed: X402_ENABLED=true without X402_PAY_TO -> still disabled,
    # still free, still not claiming live.
    monkeypatch.setenv("X402_ENABLED", "true")
    r = client.get("/.well-known/x402.json")
    body = _manifest(r)
    assert body["x402_status"] == "implemented_but_disabled"


def test_manifest_free_even_when_x402_effectively_enabled(client, monkeypatch):
    # With x402 fully enabled the middleware is armed, but the manifest must
    # still be free (never 402) and its prices unchanged.
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", "0xAbC1230000000000000000000000000000000000")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://x402.org/facilitator")
    assert x402_mod.x402_effective_enabled()
    c = TestClient(create_app())
    r = c.get("/.well-known/x402.json")
    assert r.status_code == 200, f"manifest 402-gated when x402 armed: {r.text[:200]}"
    body = r.json()
    for tool in x402_mod.TOOL_PRICE_DEFAULTS:
        assert body["tools"][tool]["price_usdc"] == str(x402_mod.tool_price_usdc(tool))


def test_manifest_pay_to_null_when_unset(client):
    body = _manifest(client.get("/.well-known/x402.json"))
    assert body["payment"]["pay_to"] is None  # never an invented address


def test_manifest_pay_to_reflects_env(client, monkeypatch):
    monkeypatch.setenv("X402_PAY_TO", "0xPayToAddress123")
    body = _manifest(client.get("/.well-known/x402.json"))
    assert body["payment"]["pay_to"] == "0xPayToAddress123"


def test_manifest_asset_and_network_identifiers_come_from_module(client):
    # Accepted asset/network identifiers must be the ones the code uses —
    # read from the module, never invented.
    body = _manifest(client.get("/.well-known/x402.json"))
    payment = body["payment"]
    assert payment["scheme"] == x402_mod.SCHEME
    assert payment["network"] == x402_mod.NETWORK
    assert payment["asset"] == "USDC"
    assert payment["asset_address"] == x402_mod.USDC_BASE_ADDRESS
    assert payment["asset_decimals"] == x402_mod.USDC_DECIMALS


def test_manifest_links_legal_endpoints(client):
    body = _manifest(client.get("/.well-known/x402.json"))
    assert body["legal"] == {
        "terms": "/legal/terms",
        "privacy": "/legal/privacy",
        "refunds": "/legal/refunds",
    }


def test_manifest_carries_tool_rest_paths_and_descriptions(client):
    body = _manifest(client.get("/.well-known/x402.json"))
    for tool, info in body["tools"].items():
        assert info["rest_path"] == next(p for p, t in x402_mod.REST_TOOL_MAP.items() if t == tool)
        assert info["description"] == x402_mod.TOOL_DESCRIPTIONS[tool]
