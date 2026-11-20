"""Tier 1 deferral tests (Card 56): XRP / XLM / HBAR / ALGO must NOT
create a settlement leg.

Verified research (2026-10-27): the configured Coinbase CDP facilitator
supports mainnets Base, Polygon, Arbitrum, Monad, World, Solana only, and
the x402.org facilitator is testnet-only for Algorand / Stellar / Hedera /
XRPL. No Tier 1 leg is built or advertised in this session.

These tests prove the deferral at the existing code's fail-closed seams
(verified in src/price_tracker/x402.py, untouched by this file):

- ``leg_config_problems()`` returns a problem for any network name that is
  neither base nor solana ("unknown x402 network ... in X402_NETWORKS").
- ``valid_networks()`` drops such a leg with an ERROR log; the remaining
  legs keep working.
- ``network_configs()`` and ``build_discovery_manifest()`` only branch on
  base and solana, so a Tier 1 name can never produce a servable config
  or a manifest entry.
- The payment middleware rejects a payload whose network has no servable
  leg with 402 ``payment_invalid`` / ``unsupported network`` before any
  facilitator verify/settle call.

All Tier 1 values here are SYNTHETIC test-only strings — never a real
address, never a facilitator URL, never a credential. The hypothetical
``X402_PAY_TO_<TIER1>`` env vars are not read by production code at all;
setting them must change nothing.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app

# Synthetic values only (same style as test_x402_solana.py).
SOL_PAY_TO = "So1anaPayToTest1111111111111111111111111111"
FEE_PAYER = "FeePayerTestSo1ana111111111111111111111111"
BASE_PAY_TO = "0x000000000000000000000000000000000000dEaD"

TIER1_NETWORKS = ("xrp", "xlm", "hbar", "algo")
TIER1_ENV = "base,solana,xrp,xlm,hbar,algo"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch DB."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


@pytest.fixture(autouse=True)
def _no_network_discovery(monkeypatch):
    """Keep the suite hermetic: fee-payer discovery is stubbed to 'no
    resolution' (the Solana leg is servable here via the env override)."""

    async def _stub_resolve():
        return None

    monkeypatch.setattr(x402_mod, "resolve_solana_fee_payer", _stub_resolve)
    monkeypatch.setattr(x402_mod, "_solana_fee_payer_cache", None)


def _enable_tier1_env(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": BASE_PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "X402_NETWORKS": TIER1_ENV,
        "X402_PAY_TO_SOLANA": SOL_PAY_TO,
        "X402_SOLANA_FEE_PAYER": FEE_PAYER,
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _enable_base_only(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": BASE_PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("X402_NETWORKS", raising=False)
    monkeypatch.delenv("X402_PAY_TO_SOLANA", raising=False)
    monkeypatch.delenv("X402_SOLANA_FEE_PAYER", raising=False)


def _price_body(client):
    return client.post("/price", json={"url": "https://example.com/"})


def _header(payload_dict):
    raw = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
    return {"X-Payment": raw}


def _fake_facilitator(monkeypatch):
    """2-arg facilitator fakes, same pattern as test_x402_solana.py."""
    calls = {"verify": [], "settle": []}

    async def fake_verify(payment_payload, requirements):
        calls["verify"].append((payment_payload, requirements))
        return (True, None, BASE_PAY_TO)

    async def fake_settle(payment_payload, requirements):
        calls["settle"].append((payment_payload, requirements))
        return {
            "success": True,
            "transaction": "TestSettleSig111",
            "network": requirements.get("network"),
            "payer": BASE_PAY_TO,
            "errorReason": None,
        }

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)
    return calls


# ------------------------------------------------- accepts / configs


def test_tier1_names_produce_no_accepts_entry(monkeypatch, tmp_path):
    _enable_tier1_env(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    body = _price_body(xc).json()

    assert isinstance(body["accepts"], list)
    assert {entry["network"] for entry in body["accepts"]} == {"base", "solana"}
    assert len(body["accepts"]) == 2
    assert isinstance(body["paymentRequirements"], list)
    assert {entry["network"] for entry in body["paymentRequirements"]} == {
        "base",
        "solana",
    }
    assert len(body["paymentRequirements"]) == 2
    # Direct config-level proof: no Tier 1 config exists to serve.
    assert set(x402_mod.network_configs()) == {"base", "solana"}
    assert set(x402_mod.valid_networks()) == {"base", "solana"}


def test_tier1_names_dropped_with_error_log(monkeypatch, tmp_path, caplog):
    _enable_tier1_env(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    with caplog.at_level(logging.ERROR):
        resp = _price_body(xc)
    assert resp.status_code == 402
    error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    for name in TIER1_NETWORKS:
        assert any(
            name in message and "unknown x402 network" in message for message in error_messages
        ), f"no ERROR drop log for {name!r}: {error_messages!r}"


def test_tier1_only_request_has_no_servable_leg(monkeypatch):
    _enable_tier1_env(monkeypatch, X402_NETWORKS="xrp,xlm,hbar,algo")
    assert x402_mod.network_configs() == {}
    assert x402_mod.valid_networks() == []


# ------------------------------------------------- discovery manifest


def test_discovery_manifest_excludes_tier1(monkeypatch, tmp_path):
    _enable_tier1_env(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    manifest = xc.get("/.well-known/x402.json").json()
    networks = [entry["network"] for entry in manifest["networks"]]
    assert networks == ["base", "solana"]
    assert not set(networks) & set(TIER1_NETWORKS)
    # Top-level payment block still describes the primary (Base) leg.
    assert manifest["payment"]["network"] == "base"
    assert manifest["payment"]["pay_to"] == BASE_PAY_TO


# ------------------------------------------------- payment rejection


@pytest.mark.parametrize("network", TIER1_NETWORKS)
def test_tier1_payment_rejected_without_facilitator_call(monkeypatch, tmp_path, network):
    _enable_tier1_env(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    calls = _fake_facilitator(monkeypatch)
    xc = TestClient(create_app())
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {},
    }
    resp = xc.post("/price", json={"url": "https://example.com/"}, headers=_header(payload))
    assert resp.status_code == 402
    body = resp.json()
    assert body["error"] == "payment_invalid"
    assert body["reason"] == "unsupported network"
    # Rejected by the existing unsupported-network path: never verified
    # or settled, so no facilitator call of any kind was made.
    assert calls["verify"] == []
    assert calls["settle"] == []


# ------------------------------------------------- hypothetical pay-to env


def test_hypothetical_tier1_payto_env_creates_no_leg(monkeypatch, tmp_path):
    _enable_tier1_env(monkeypatch)
    # These env vars are not read by production code; they must not
    # conjure a leg, a manifest entry, or an accepts entry.
    for name in TIER1_NETWORKS:
        monkeypatch.setenv(
            f"X402_PAY_TO_{name.upper()}", f"SYNTHETIC-TEST-ONLY-{name.upper()}-PAYTO"
        )
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    body = _price_body(xc).json()
    assert {entry["network"] for entry in body["accepts"]} == {"base", "solana"}
    manifest = xc.get("/.well-known/x402.json").json()
    assert [entry["network"] for entry in manifest["networks"]] == ["base", "solana"]
    assert set(x402_mod.network_configs()) == {"base", "solana"}


# ------------------------------------------------- default Base-only unchanged


def test_default_base_only_challenge_unchanged(monkeypatch, tmp_path):
    _enable_base_only(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    body = _price_body(xc).json()
    assert body["error"] == "payment_required"
    # Single-network shape: dict, not list — exactly as before.
    assert isinstance(body["accepts"], dict)
    assert body["accepts"]["network"] == "base"
    assert body["accepts"]["asset_contract"] == x402_mod.USDC_BASE_ADDRESS
    assert body["accepts"]["pay_to"] == BASE_PAY_TO
    assert isinstance(body["paymentRequirements"], dict)
    assert body["paymentRequirements"]["network"] == "base"


def test_default_manifest_base_only(monkeypatch, tmp_path):
    _enable_base_only(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    xc = TestClient(create_app())
    manifest = xc.get("/.well-known/x402.json").json()
    assert [entry["network"] for entry in manifest["networks"]] == ["base"]
    assert manifest["networks"][0]["pay_to"] == BASE_PAY_TO
    assert manifest["payment"]["network"] == "base"
    assert manifest["payment"]["pay_to"] == BASE_PAY_TO
