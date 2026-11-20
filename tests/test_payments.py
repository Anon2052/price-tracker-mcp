"""Tests for real x402 verification, per-tool pricing, anti-replay, CDP JWT,
and Stripe billing (checkout/webhook/subscriber keys). Payments OFF by default.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
import stripe
from fastapi.testclient import TestClient

from price_tracker import billing, x402
from price_tracker.rest_app import create_app

PAY_TO = "0xAbC1230000000000000000000000000000000000"
PAYER = "0xDeF4560000000000000000000000000000000000"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.fixture(autouse=True)
def _isolated_billing_db(monkeypatch, tmp_path):
    """M-1: the replay table is persistent — point every test at a scratch
    DB so claims can never leak between tests (or touch the real path)."""
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "billing.db"))


# ------------------------------------------------------------------ helpers


def _enable_x402(monkeypatch, **over):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("X402_PAY_TO", over.get("pay_to", PAY_TO))
    monkeypatch.setenv(
        "X402_FACILITATOR_URL", over.get("facilitator", "https://x402.test/facilitator")
    )
    # These tests exercise the pure x402 challenge flow, so the free tier is
    # pinned off (it would otherwise serve anonymous calls from the bucket).
    monkeypatch.setenv("FREE_TIER_ENABLED", "0")
    for k, v in over.get("prices", {}).items():
        monkeypatch.setenv(k, v)


def _x_payment_header(
    *,
    to=PAY_TO,
    value="20000",
    valid_after=None,
    valid_before=None,
    signature="0xSig789",
    scheme="exact",
    network="base",
    version=1,
):
    now = int(time.time())
    payload = {
        "x402Version": version,
        "scheme": scheme,
        "network": network,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": PAYER,
                "to": to,
                "value": value,
                "validAfter": str(valid_after if valid_after is not None else now - 60),
                "validBefore": str(valid_before if valid_before is not None else now + 240),
                "nonce": "0xnonce1",
            },
        },
    }
    return {"X-Payment": base64.b64encode(json.dumps(payload).encode()).decode()}


@pytest.fixture()
def priced_app(monkeypatch, tmp_path):
    """App with x402 enabled (facilitator mocked), Stripe disabled."""
    _enable_x402(monkeypatch)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    x402._reset_replay_cache()
    # Enter the lifespan so the MCP sub-app's task group is initialized.
    with TestClient(create_app()) as client:
        yield client


def _mock_facilitator(monkeypatch, verify_result=(True, None, PAYER), settle_result=None):
    async def fake_verify(payment_payload, requirements):
        return verify_result

    async def fake_settle(payment_payload, requirements):
        return settle_result or {
            "success": True,
            "transaction": "0xTxHash",
            "network": "base",
            "payer": PAYER,
            "errorReason": None,
        }

    monkeypatch.setattr(x402, "facilitator_verify", fake_verify)
    monkeypatch.setattr(x402, "facilitator_settle", fake_settle)


def _mock_facilitator_down(monkeypatch):
    async def fake_verify(payment_payload, requirements):
        raise x402.FacilitatorUnavailable("connection refused")

    monkeypatch.setattr(x402, "facilitator_verify", fake_verify)


# ------------------------------------------------- disabled-by-default & 402


def test_disabled_by_default_serves_without_payment(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    c = TestClient(create_app())
    r = c.post("/price", json={"url": "https://example.com/"})
    assert r.status_code in (200, 422)  # 422 = price backend failed, not 402


def test_402_challenge_shape(priced_app):
    r = priced_app.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required"
    assert body["accepts"]["scheme"] == "exact"
    assert "stub" not in json.dumps(body).lower()
    reqs = body["paymentRequirements"]
    assert reqs["asset"] == USDC
    assert reqs["network"] == "base"
    assert reqs["payTo"] == PAY_TO
    # check_price default $0.008 -> 8000 atomic units
    assert reqs["maxAmountRequired"] == "8000"


def test_402_canonical_shape(priced_app):
    """The 402 body uses the canonical cross-repo shape: error/message/
    accepts/paymentRequirements, with reason present on rejections."""
    # Bare challenge: no reason key when there is no rejection detail.
    r = priced_app.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_required"
    assert "x402 USDC micropayment" in body["message"]
    assert "reason" not in body
    assert body["accepts"] == {
        "asset": "USDC",
        "asset_contract": USDC,
        "network": "base",
        "amount_usdc": "0.008",
        "amount_atomic": "8000",
        "pay_to": PAY_TO,
        "scheme": "exact",
    }
    assert body["paymentRequirements"]["maxAmountRequired"] == "8000"

    # Rejection: reason present, challenge still attached.
    r = priced_app.post(
        "/price",
        json={"url": "https://example.com/"},
        headers=_x_payment_header(network="ethereum"),
    )
    assert r.status_code == 402
    body = r.json()
    assert body["error"] == "payment_invalid"
    assert body["reason"] == 'unsupported network (want "base")'
    assert "paymentRequirements" in body


def test_per_tool_pricing_env_override(monkeypatch, tmp_path):
    _enable_x402(monkeypatch, prices={"X402_PRICE_CHECK_PRICE_USDC": "0.05"})
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    c = TestClient(create_app())
    r = c.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "50000"
    # affiliate tool keeps its own default $0.005 -> 5000 atomic
    r2 = c.post("/affiliate", json={"url": "https://www.amazon.com/dp/B0X", "tag": "t-20"})
    assert r2.status_code == 402
    assert r2.json()["paymentRequirements"]["maxAmountRequired"] == "5000"


def test_invalid_price_env_falls_back_to_default(monkeypatch, tmp_path):
    _enable_x402(monkeypatch, prices={"X402_PRICE_CHECK_PRICE_USDC": "not-a-number"})
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    c = TestClient(create_app())
    r = c.post("/price", json={"url": "https://example.com/"})
    assert r.status_code == 402
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "8000"


def test_jsonrpc_discovery_exempt(priced_app):
    r = priced_app.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200  # not 402


def test_jsonrpc_tools_call_is_paid(priced_app):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "check_price", "arguments": {"url": "https://example.com/"}},
    }
    r = priced_app.post("/mcp", json=body)
    assert r.status_code == 402
    assert r.json()["paymentRequirements"]["maxAmountRequired"] == "8000"


# ------------------------------------------------- decode + sanity checks


def test_bad_base64_gets_challenge(priced_app, monkeypatch):
    _mock_facilitator(monkeypatch)
    r = priced_app.post(
        "/price", json={"url": "https://example.com/"}, headers={"X-Payment": "!!!"}
    )
    assert r.status_code == 402
    assert r.json()["error"] == "payment_required"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"to": "0xWrong000000000000000000000000000000000000"},
        {"value": "1"},  # below 8000 atomic required
        {"network": "ethereum"},
        {"scheme": "upto"},
        {"version": 2},
    ],
)
def test_sanity_failures_402(priced_app, monkeypatch, kwargs):
    _mock_facilitator(monkeypatch)
    now = int(time.time())
    if "valid_before" not in kwargs:
        kwargs = dict(kwargs, valid_before=now + 240)
    r = priced_app.post(
        "/price", json={"url": "https://example.com/"}, headers=_x_payment_header(**kwargs)
    )
    assert r.status_code == 402
    assert r.json()["error"] == "payment_invalid"


def test_expired_authorization_402(priced_app, monkeypatch):
    _mock_facilitator(monkeypatch)
    now = int(time.time())
    r = priced_app.post(
        "/price",
        json={"url": "https://example.com/"},
        headers=_x_payment_header(valid_after=now - 600, valid_before=now - 60),
    )
    assert r.status_code == 402
    assert r.json()["error"] == "payment_invalid"


# ------------------------------------------------- facilitator outcomes


def _stub_price_backend(monkeypatch):
    from price_tracker import price as price_mod

    monkeypatch.setattr(price_mod, "fetch_html", lambda url, timeout=20: "<html></html>")


def test_verify_false_402_with_reason(priced_app, monkeypatch):
    _mock_facilitator(monkeypatch, verify_result=(False, "bad signature", PAYER))
    _stub_price_backend(monkeypatch)
    r = priced_app.post("/price", json={"url": "https://example.com/"}, headers=_x_payment_header())
    assert r.status_code == 402
    assert r.json()["error"] == "payment_invalid"
    assert r.json()["reason"] == "bad signature"


def test_verify_true_serves_and_settles(priced_app, monkeypatch, caplog):
    _mock_facilitator(monkeypatch)
    from price_tracker import price as price_mod

    monkeypatch.setattr(
        price_mod,
        "fetch_html",
        lambda url, timeout=20: (
            "<html><head><title>W</title>"
            '<meta property="og:price:amount" content="9.99" /></head></html>'
        ),
    )
    with caplog.at_level("INFO", logger="price_tracker.x402"):
        r = priced_app.post(
            "/price", json={"url": "https://example.com/"}, headers=_x_payment_header()
        )
    assert r.status_code == 200
    assert r.json()["price"] == pytest.approx(9.99)
    assert any("0xTxHash" in rec.message for rec in caplog.records)


def test_settle_failure_still_serves(priced_app, monkeypatch, caplog):
    _mock_facilitator(
        monkeypatch,
        settle_result={
            "success": False,
            "transaction": None,
            "network": "base",
            "payer": PAYER,
            "errorReason": "rpc blew up",
        },
    )
    _stub_price_backend(monkeypatch)
    with caplog.at_level("ERROR", logger="price_tracker.x402"):
        r = priced_app.post(
            "/price",
            json={"url": "https://example.com/"},
            headers=_x_payment_header(signature="0xUniqueSettle"),
        )
    assert r.status_code in (200, 422)  # served, not failed by settle
    assert any("SETTLE FAILED" in rec.message for rec in caplog.records)


def test_facilitator_unreachable_503(priced_app, monkeypatch):
    _mock_facilitator_down(monkeypatch)
    r = priced_app.post(
        "/price",
        json={"url": "https://example.com/"},
        headers=_x_payment_header(signature="0xDown1"),
    )
    assert r.status_code == 503
    assert r.json()["error"] == "payment_verification_unavailable"


def test_replay_second_use_402(priced_app, monkeypatch):
    _mock_facilitator(monkeypatch)
    _stub_price_backend(monkeypatch)
    headers = _x_payment_header(signature="0xReplayMe")
    r1 = priced_app.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r1.status_code in (200, 422)
    r2 = priced_app.post("/price", json={"url": "https://example.com/"}, headers=headers)
    assert r2.status_code == 402
    assert r2.json()["error"] == "duplicate_payment"


def test_x402_flag_on_but_incomplete_config_stays_disabled(monkeypatch, tmp_path):
    # X402_ENABLED=true without X402_PAY_TO -> fail closed: no 402s.
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    assert not x402.x402_effective_enabled()
    c = TestClient(create_app())
    assert c.get("/health").json()["x402"] == "disabled"
    r = c.post("/price", json={"url": "https://example.com/"})
    assert r.status_code != 402


# ------------------------------------------------- CDP JWT


def _ed25519_keypair():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def test_mint_cdp_jwt_structure_and_signature():
    from cryptography.hazmat.primitives import serialization

    priv, pub = _ed25519_keypair()
    raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    import base64 as _b64

    secret_b64 = _b64.b64encode(raw).decode()
    now = int(time.time())
    jwt = x402.mint_cdp_jwt("key-id-1", secret_b64, now=now)
    header_b64, claims_b64, sig_b64 = jwt.split(".")

    def dec(part):
        return json.loads(_b64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    header, claims = dec(header_b64), dec(claims_b64)
    assert header["alg"] == "EdDSA"
    assert header["typ"] == "JWT"
    assert header["kid"] == "key-id-1"
    assert len(header["nonce"]) == 32
    assert claims["sub"] == "key-id-1"
    assert claims["iss"] == "coinbase-cloud"
    assert claims["nbf"] == now - 30
    assert claims["exp"] == now + 120
    # Signature verifies against the derived public key.
    signing_input = f"{header_b64}.{claims_b64}".encode()
    signature = _b64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    pub.verify(signature, signing_input)  # raises on failure


def test_mint_cdp_jwt_accepts_hex_and_rejects_garbage():
    from cryptography.hazmat.primitives import serialization

    priv, _ = _ed25519_keypair()
    seed_hex = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    jwt = x402.mint_cdp_jwt("k", seed_hex)
    assert jwt.count(".") == 2
    with pytest.raises(ValueError):
        x402.mint_cdp_jwt("k", "not-a-key")


# ------------------------------------------------- Stripe


def _signed_webhook_headers(payload: bytes) -> dict:
    sig_header = stripe.WebhookSignature.generate_signature_header(
        payload.decode("utf-8"), "whsec_test_123"
    )
    return {"stripe-signature": sig_header}


def _enable_stripe(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_ENABLED", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_123")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_test_123")
    monkeypatch.setenv("STRIPE_SUCCESS_URL", "https://example.com/ok")
    monkeypatch.setenv("STRIPE_CANCEL_URL", "https://example.com/cancel")
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))


def test_stripe_disabled_checkout_404(monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    c = TestClient(create_app())
    r = c.post("/billing/checkout")
    assert r.status_code == 404
    assert c.get("/billing/status").json() == {"stripe_enabled": False, "x402_enabled": False}


def test_webhook_completed_creates_subscriber_hashed(monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    c = TestClient(create_app())

    payload = json.dumps(
        {
            "id": "evt_1",
            "type": "checkout.session.completed",
            "data": {"object": {"customer": "cus_123", "mode": "subscription",
                          "customer_details": {"email": "a@b.c"}}},
        }
    ).encode()
    r = c.post(
        "/billing/stripe-webhook",
        content=payload,
        headers=_signed_webhook_headers(payload),
    )
    assert r.status_code == 200, r.text
    rows = billing.get_subscriber_rows("cus_123")
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["email"] == "a@b.c"
    # Stored value is a sha256 hash, not a raw token.
    assert len(rows[0]["key_hash"]) == 64
    assert rows[0]["key_hash"] != rows[0].get("token")


def test_webhook_deleted_deactivates(monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    token = billing.create_subscriber("cus_9", "x@y.z")
    assert billing.valid_subscriber_key(token)
    c = TestClient(create_app())
    payload = json.dumps(
        {
            "id": "evt_2",
            "type": "customer.subscription.deleted",
            "data": {"object": {"customer": "cus_9"}},
        }
    ).encode()
    r = c.post(
        "/billing/stripe-webhook",
        content=payload,
        headers=_signed_webhook_headers(payload),
    )
    assert r.status_code == 200
    assert not billing.valid_subscriber_key(token)
    assert billing.get_subscriber_rows("cus_9")[0]["status"] == "canceled"


def test_billing_key_exchanges_paid_session(monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    c = TestClient(create_app())

    def fake_retrieve(session_id, expand=None):
        assert session_id == "cs_test_1"
        return {
            "id": "cs_test_1",
            "status": "complete",
            "mode": "subscription",
            "payment_status": "paid",
            "customer": "cus_key2",
            "customer_details": {"email": "k2@e.y"},
            # M-2: the session must carry a line item for the configured price.
            "line_items": {"data": [{"price": {"id": "price_test_123"}}]},
        }

    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake_retrieve)
    r = c.get("/billing/key", params={"session_id": "cs_test_1"})
    assert r.status_code == 200, r.text
    token = r.json()["subscriber_key"]
    assert len(token) == 64
    assert billing.valid_subscriber_key(token)  # the issued key works


def test_billing_key_rejects_unpaid_session(monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    c = TestClient(create_app())

    def fake_retrieve(session_id, expand=None):
        return {"id": session_id, "status": "open", "payment_status": "unpaid"}

    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake_retrieve)
    r = c.get("/billing/key", params={"session_id": "cs_test_2"})
    assert r.status_code == 402


def test_webhook_bad_signature_400(monkeypatch, tmp_path):
    _enable_stripe(monkeypatch, tmp_path)
    c = TestClient(create_app())
    r = c.post(
        "/billing/stripe-webhook",
        content=b"{}",
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 400


def test_subscriber_key_bypasses_x402(priced_app, monkeypatch, tmp_path):
    monkeypatch.setenv("BILLING_DB_PATH", str(tmp_path / "sub.db"))
    _mock_facilitator(monkeypatch)  # would 402/serve; key must skip it entirely
    _stub_price_backend(monkeypatch)
    token = billing.create_subscriber("cus_key", "k@e.y")
    r = priced_app.post(
        "/price", json={"url": "https://example.com/"}, headers={"X-Subscriber-Key": token}
    )
    assert r.status_code in (200, 422)  # served, no 402
    # Bad key falls through to the x402 flow -> 402.
    r2 = priced_app.post(
        "/price", json={"url": "https://example.com/"}, headers={"X-Subscriber-Key": "bogus"}
    )
    assert r2.status_code == 402


def test_health_reports_effective_state(monkeypatch, tmp_path):
    _enable_x402(monkeypatch)
    _enable_stripe(monkeypatch, tmp_path)
    c = TestClient(create_app())
    body = c.get("/health").json()
    assert body["x402"] == "enabled"
    assert body["stripe"] == "enabled"
    assert "sk_test" not in json.dumps(body)
    status = c.get("/billing/status").json()
    assert status == {"stripe_enabled": True, "x402_enabled": True}
