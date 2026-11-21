"""Bazaar discovery metadata (manifest-hygiene pass, 2026-10-21).

Covers: the discovery manifest served at BOTH /.well-known/x402 and
/.well-known/x402.json with identical content; x402 v1 ``outputSchema``
inside paymentRequirements; x402 v2 ``extensions.bazaar`` on 402
challenges; and the additive-only guarantee (legacy challenge fields and
free-tier behavior unchanged).

Format references (verified against primary sources 2026-10-21): the x402
Python SDK (x402/extensions/bazaar — ``declare_discovery_extension`` /
``declare_mcp_discovery_extension`` in resource_service.py, types.py, and
the "V1 Support" extractor contract) and CDP docs "Get discovered
(Bazaar)".
"""

from decimal import Decimal

import jsonschema
import pytest
from fastapi.testclient import TestClient

from price_tracker import price as price_mod
from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app

PAY_TO = "0xAbC1230000000000000000000000000000000000"
PRODUCT_URL = "https://www.example.com/product"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


def _enable(monkeypatch, **over):
    env = {
        "X402_ENABLED": "true",
        "X402_PAY_TO": PAY_TO,
        "X402_FACILITATOR_URL": "https://x402.test/facilitator",
        "FREE_TIER_ENABLED": "false",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _validate_bazaar(extension):
    """The extension's info block must validate against its own schema."""
    jsonschema.validate(extension["info"], extension["schema"])
    assert extension["schema"]["required"] == ["input"]


# ------------------------------------------------------------- manifest


def test_manifest_served_at_both_paths_identically():
    xc = TestClient(create_app())
    bare = xc.get("/.well-known/x402")
    dotted = xc.get("/.well-known/x402.json")
    assert bare.status_code == dotted.status_code == 200
    assert bare.json() == dotted.json()
    tools = bare.json()["tools"]
    assert tools["check_price"]["price_usdc"] == "0.008"
    assert tools["check_price"]["rest_path"] == "/price"
    assert tools["make_affiliate_link"]["rest_path"] == "/affiliate"


# ------------------------------------------------------- v1 outputSchema


def test_requirements_carry_output_schema_for_rest_routes():
    req = x402_mod.build_payment_requirements(
        Decimal("0.008"),
        "https://prices.ioconnex.dev/price",
        x402_mod.TOOL_DESCRIPTIONS["check_price"],
        PAY_TO,
    )
    schema = req["outputSchema"]
    assert schema["input"]["type"] == "http"
    assert schema["input"]["method"] == "POST"
    assert schema["input"]["discoverable"] is True
    assert schema["input"]["bodyFields"] == {"url": PRODUCT_URL}
    assert "price" in schema["output"]["required"]


def test_requirements_carry_output_schema_for_mcp_tool_call():
    # No explicit tool: the description prefix identifies it.
    req = x402_mod.build_payment_requirements(
        Decimal("0.005"),
        "https://prices.ioconnex.dev/mcp",
        x402_mod.TOOL_DESCRIPTIONS["make_affiliate_link"],
        PAY_TO,
    )
    body = req["outputSchema"]["input"]["bodyFields"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "make_affiliate_link"
    assert body["params"]["arguments"]["tag"] == "mytag-20"
    assert req["outputSchema"]["output"] == {"type": "string"}


def test_no_output_schema_for_uncatalogued_resources():
    req = x402_mod.build_payment_requirements(
        Decimal("0.01"), "https://prices.ioconnex.dev/other", "/other", PAY_TO
    )
    assert "outputSchema" not in req


# ------------------------------------------------------ v2 extensions


def test_extensions_bazaar_http_and_mcp_forms_validate():
    http_ext = x402_mod._bazaar_extension("https://prices.ioconnex.dev/affiliate", "")
    _validate_bazaar(http_ext)
    assert http_ext["info"]["input"]["type"] == "http"
    assert http_ext["info"]["input"]["method"] == "POST"
    assert http_ext["info"]["input"]["body"]["tag"] == "mytag-20"

    mcp_ext = x402_mod._bazaar_extension(
        "https://prices.ioconnex.dev/mcp", "", tool_name="check_price"
    )
    _validate_bazaar(mcp_ext)
    assert mcp_ext["info"]["input"]["type"] == "mcp"
    assert mcp_ext["info"]["input"]["toolName"] == "check_price"
    assert mcp_ext["info"]["input"]["transport"] == "streamable-http"
    assert mcp_ext["info"]["input"]["example"] == {"url": PRODUCT_URL}
    assert mcp_ext["info"]["input"]["inputSchema"]["required"] == ["url"]


def test_challenge_carries_extensions_for_rest_route(monkeypatch):
    _enable(monkeypatch)
    xc = TestClient(create_app())
    resp = xc.post("/price", json={"url": PRODUCT_URL})
    assert resp.status_code == 402
    body = resp.json()
    # Legacy challenge fields unchanged.
    assert body["error"] == "payment_required"
    assert body["accepts"]["pay_to"] == PAY_TO
    assert body["accepts"]["amount_usdc"] == "0.008"
    assert body["paymentRequirements"]["outputSchema"]["input"]["method"] == "POST"
    # New additive block.
    bazaar = body["extensions"]["bazaar"]
    _validate_bazaar(bazaar)
    assert bazaar["info"]["input"]["type"] == "http"
    assert bazaar["info"]["input"]["body"] == {"url": PRODUCT_URL}


def test_challenge_carries_extensions_for_mcp_tool_call(monkeypatch):
    _enable(monkeypatch)
    xc = TestClient(create_app())
    resp = xc.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "make_affiliate_link",
                "arguments": {"url": PRODUCT_URL, "tag": "mytag-20"},
            },
        },
    )
    assert resp.status_code == 402
    bazaar = resp.json()["extensions"]["bazaar"]
    _validate_bazaar(bazaar)
    assert bazaar["info"]["input"]["type"] == "mcp"
    assert bazaar["info"]["input"]["toolName"] == "make_affiliate_link"


def test_challenge_without_resource_has_no_extensions():
    requirements = {
        "maxAmountRequired": "8000",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "network": "base",
        "payTo": PAY_TO,
        "scheme": "exact",
    }
    content = x402_mod.challenge_content("payment_required", requirements)
    assert "extensions" not in content


def test_free_tier_behavior_unchanged(monkeypatch):
    _enable(monkeypatch, FREE_TIER_ENABLED="true")
    monkeypatch.setattr(
        price_mod,
        "check_price",
        lambda url: price_mod.PriceInfo(
            price=42.0, currency="USD", title="T", availability=None, source="test"
        ),
    )
    xc = TestClient(create_app())
    resp = xc.post("/price", json={"url": PRODUCT_URL})
    assert resp.status_code == 200
    assert resp.json()["price"] == 42.0
