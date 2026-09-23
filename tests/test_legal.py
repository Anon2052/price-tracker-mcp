"""Tests for the public legal document endpoints (/legal/*).

The docs must be served verbatim (placeholders unfilled), as text/markdown,
never rate-limited, and never subject to x402 payment — even when x402 is on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from price_tracker.legal_docs import PRIVACY_POLICY, REFUND_POLICY, TERMS_OF_SERVICE
from price_tracker.rest_app import create_app

_DOCS = {
    "/legal/terms": (TERMS_OF_SERVICE, "# Terms of Service"),
    "/legal/privacy": (PRIVACY_POLICY, "# Privacy Policy"),
    "/legal/refunds": (REFUND_POLICY, "# Refund Policy"),
}


@pytest.fixture()
def client():
    return TestClient(create_app())


def test_legal_routes_serve_verbatim_markdown(client):
    for path, (doc, heading) in _DOCS.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/markdown" in r.headers["content-type"], path
        assert r.text == doc, f"{path} body differs from the legal_docs constant"
        assert heading in r.text, path
        # Placeholders stay unfilled: proves the served doc is the verbatim template.
        assert "[LLC NAME]" in r.text, path


def test_legal_routes_never_402_when_x402_enabled(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", "0xAbC123")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://example.com/facilitator")
    xc = TestClient(create_app())
    for path in _DOCS:
        r = xc.get(path)
        assert r.status_code == 200, path
    # Proof x402 is actually enforced in this app: an unpaid paid-route call 402s.
    r = xc.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402


def test_legal_routes_exempt_from_rate_limit():
    limited = TestClient(create_app(rate_limit="5/minute"))
    for _ in range(10):
        r = limited.get("/legal/terms")
        assert r.status_code == 200, f"legal route hit rate limit: {r.status_code}"
    # Sanity: the same limit still bites a metered route (/affiliate is offline-safe).
    codes = set()
    for _ in range(10):
        r = limited.post(
            "/affiliate",
            json={"url": "https://www.amazon.com/dp/B0X", "tag": "mytag-20"},
        )
        codes.add(r.status_code)
    assert 429 in codes, f"expected a 429 on /affiliate, got {codes}"
