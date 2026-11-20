"""Landing page at /: the ioconnex.dev "superpowers" links point at the bare
service root, so it must serve the live-demo page publicly and free — and the
x402 exemption for "/" must stay exact-path (not widen to other routes)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from price_tracker.rest_app import create_app


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


def test_landing_page_served():
    with TestClient(create_app()) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Price Tracker" in r.text
    assert "Check a product price, live." in r.text


def test_landing_exemption_does_not_widen():
    with TestClient(create_app()) as client:
        r = client.get("/definitely-not-a-route")
    assert r.status_code == 404


def test_support_page_still_served():
    with TestClient(create_app()) as client:
        r = client.get("/support")
    assert r.status_code == 200
