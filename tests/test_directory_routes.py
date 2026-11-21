"""Directory routes: /support page, OpenAI domain-verification challenge,
and the review demo-video route — all public and always free."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from price_tracker.rest_app import create_app


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


def test_support_page_served():
    with TestClient(create_app()) as client:
        r = client.get("/support")
    assert r.status_code == 200
    assert "IOConnex Price Tracker" in r.text
    assert "hello@ioconnex.dev" in r.text


def test_challenge_404_until_token_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_APPS_CHALLENGE_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        r = client.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 404


def test_challenge_serves_bare_token_when_configured(monkeypatch):
    monkeypatch.setenv("OPENAI_APPS_CHALLENGE_TOKEN", "tok-test-123")
    with TestClient(create_app()) as client:
        r = client.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200
    assert r.text == "tok-test-123"
    assert r.headers["content-type"].startswith("text/plain")


def test_demo_video_404_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_VIDEO_PATH", str(tmp_path / "missing.mp4"))
    with TestClient(create_app()) as client:
        r = client.get("/demo-walkthrough.mp4")
    assert r.status_code == 404


def test_demo_video_served_when_present(monkeypatch, tmp_path):
    video = tmp_path / "demo.mp4"
    payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1024
    video.write_bytes(payload)
    monkeypatch.setenv("DEMO_VIDEO_PATH", str(video))
    with TestClient(create_app()) as client:
        r = client.get("/demo-walkthrough.mp4")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("video/mp4")
        assert r.content == payload
        r2 = client.get("/demo-walkthrough.mp4", headers={"Range": "bytes=0-99"})
        assert r2.status_code == 206
        assert len(r2.content) == 100
