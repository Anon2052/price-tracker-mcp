"""Tests for the REST API: /health, /price, /affiliate, rate limiting, x402 hook, MCP paths."""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from price_tracker import price as price_mod
from price_tracker import x402 as x402_mod
from price_tracker.rest_app import create_app

_OG_HTML = """<html><head>
<title>Widget Pro</title>
<meta property="og:price:amount" content="79.99" />
<meta property="og:price:currency" content="USD" />
</head><body><h1>Widget Pro</h1></body></html>"""


@pytest.fixture()
def stubbed_backends(monkeypatch):
    monkeypatch.setattr(price_mod, "fetch_html", lambda url, timeout=20: _OG_HTML)


@pytest.fixture()
def client(stubbed_backends):
    return TestClient(create_app())


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["x402"] == "disabled"
    assert body["stripe"] == "disabled"


def test_price_endpoint(client):
    r = client.post("/price", json={"url": "https://example.com/product"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] == pytest.approx(79.99)
    assert body["currency"] == "USD"
    assert body["title"] == "Widget Pro"


def test_price_failure_returns_422(stubbed_backends, monkeypatch):
    def boom(url, timeout=20):
        from price_tracker.fetch import FetchError

        raise FetchError("URL: host 'x' resolves to a non-public IP (127.0.0.1)")

    monkeypatch.setattr(price_mod, "fetch_html", boom)
    c = TestClient(create_app())
    r = c.post("/price", json={"url": "http://127.0.0.1/"})
    assert r.status_code == 422
    assert r.json()["error"] == "price_not_found"


def test_price_no_price_found_returns_422(stubbed_backends, monkeypatch):
    monkeypatch.setattr(price_mod, "fetch_html", lambda url, timeout=20: "<html></html>")
    c = TestClient(create_app())
    r = c.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 422


def test_affiliate_endpoint(client):
    r = client.post(
        "/affiliate",
        json={"url": "https://www.amazon.com/dp/B0X", "tag": "mytag-20"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://www.amazon.com/dp/B0X?tag=mytag-20"


def test_affiliate_bad_tag_returns_422(client):
    r = client.post(
        "/affiliate",
        json={"url": "https://www.amazon.com/dp/B0X", "tag": "bad tag!"},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "bad_affiliate_link"


def test_rate_limiting(stubbed_backends):
    limited = TestClient(create_app(rate_limit="2/minute"))
    for _ in range(2):
        assert limited.get("/health").status_code == 200  # /health not rate-limited
    codes = set()
    for _ in range(4):
        r = limited.post("/price", json={"url": "https://example.com/"})
        codes.add(r.status_code)
    assert 429 in codes, f"expected a 429, got {codes}"
    assert limited.get("/health").status_code == 200


# ------------------------------------------------------------- x402 hook


def _enable_x402(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PRICE_USDC", "0.015")
    monkeypatch.setenv("X402_PAY_TO", "0xAbC123")


def _payment_header(signature="0xSig789", **auth_over):
    # Real x402 v1 format: base64-encoded JSON EIP-3009 authorization payload.
    now = int(time.time())
    auth = {
        "from": "0xDeF456",
        "to": "0xAbC123",
        "value": "20000",
        "validAfter": str(now - 60),
        "validBefore": str(now + 300),
        "nonce": "0xnonce1",
    }
    auth.update(auth_over)
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {"signature": signature, "authorization": auth},
    }
    return {"X-Payment": base64.b64encode(json.dumps(payload).encode()).decode()}


def _mock_verify_ok(monkeypatch):
    async def fake_verify(payment_payload, requirements):
        return True, None, "0xDeF456"

    async def fake_settle(payment_payload, requirements):
        return {
            "success": True,
            "transaction": "0xTx",
            "network": "base",
            "payer": "0xDeF456",
            "errorReason": None,
        }

    monkeypatch.setattr(x402_mod, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402_mod, "facilitator_settle", fake_settle)
    x402_mod._reset_replay_cache()


def test_x402_disabled_by_default_passes_through(client):
    xc = TestClient(create_app())
    assert xc.get("/health").status_code == 200


def test_x402_enabled_blocks_without_payment(monkeypatch):
    _enable_x402(monkeypatch)
    xc = TestClient(create_app())
    r = xc.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402
    assert xc.get("/health").status_code == 200


def test_x402_enabled_accepts_facilitator_verified_payment(stubbed_backends, monkeypatch):
    _enable_x402(monkeypatch)
    _mock_verify_ok(monkeypatch)
    xc = TestClient(create_app())
    r = xc.post(
        "/price",
        json={"url": "https://example.com/"},
        headers=_payment_header(signature="0xRestOk1"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["price"] == pytest.approx(79.99)


# ------------------------------------------------------------- MCP mount paths


def _mcp_ping_body():
    return {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def _mcp_result(response):
    # Streamable HTTP answers as SSE: extract the JSON payload of the message event.
    import json

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no data event in MCP response: {response.text!r}")


def test_mcp_bare_path_does_not_redirect():
    # Bare /mcp (no trailing slash) must be served directly: many MCP clients
    # don't follow the 307 Starlette's mount would otherwise issue.
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post("/mcp", json=_mcp_ping_body())
    assert r.status_code != 307, f"bare /mcp 307-redirected to {r.headers.get('location')}"
    assert _mcp_result(r)["result"] == {}


def test_mcp_trailing_slash_path_still_works():
    with TestClient(create_app(), follow_redirects=False) as c:
        r = c.post("/mcp/", json=_mcp_ping_body())
    assert _mcp_result(r)["result"] == {}
