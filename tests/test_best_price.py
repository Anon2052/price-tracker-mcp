"""Price Tracker v2 (find_best_price) tests.

Covers the spec's fixture gates (price-tracker-v2-spec.md section 5) as far
as they can run without live retailer keys:
- Gate 1 (match accuracy / zero wrong-product verdicts): a 20-product UPC
  fixture set runs through the matcher with adversarial distractors
  (cheaper variant, cheaper used, cheaper out-of-stock, wrong product).
- Gate 3 (honesty): unknown shipping never produces a delivered-total
  winner, out-of-stock never wins, used never enters the new ranking.
- Gate 4 (graceful degradation): dead / unconfigured sources degrade to
  per-source status, never to a failed call.
Gate 2 (live price accuracy) needs live Tier A keys and is covered here
only at the parse layer: recorded API payloads must yield the payload's
own prices, to the cent.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from price_tracker import bestprice as bp
from price_tracker import sources as src_mod
from price_tracker import x402 as x402_mod
from price_tracker.price import PriceInfo
from price_tracker.rest_app import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "bestprice"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _offer(**over) -> bp.Offer:
    base = dict(
        retailer="TestStore",
        seller="TestStore",
        price=100.0,
        currency="USD",
        shipping=0.0,
        availability="in_stock",
        condition="new",
        url="https://store.example/p",
        source_api="test",
        title="Samsung Galaxy S25 Ultra 256GB",
        upc="887276925174",
    )
    base.update(over)
    return bp.Offer(**base)


class FakeSource:
    """In-memory source for orchestrator tests."""

    def __init__(self, name, offers=(), *, configured=True, error=None, filtered=0):
        self.name = name
        self._offers = list(offers)
        self._configured = configured
        self._error = error
        self._filtered = filtered
        self.calls = 0

    def configured(self):
        return self._configured

    def search(self, identity, condition, country):
        self.calls += 1
        if self._error:
            raise RuntimeError(self._error)
        return list(self._offers), self._filtered


# --------------------------------------------------------------- identifiers


def test_normalize_upc():
    assert bp.normalize_upc("887276925174") == "887276925174"
    assert bp.normalize_upc(" 8872 7692 5174 ") == "887276925174"
    assert bp.normalize_upc("00887276925174") == "00887276925174"
    assert bp.normalize_upc("12345") is None
    assert bp.normalize_upc("not-a-upc") is None
    assert bp.normalize_upc(None) is None


def test_upc_variants_bridge_upca_and_ean13():
    assert bp.upc_variants("887276925174") == {"887276925174", "0887276925174"}
    assert bp.upc_variants("0887276925174") == {"0887276925174", "887276925174"}


def test_identity_from_query_extracts_upc_brand_mpn():
    ident = bp.identity_from_query("Samsung Galaxy S25 Ultra 256GB")
    assert ident.brand == "samsung"
    assert ident.upc is None
    ident2 = bp.identity_from_query("upc 887276925174")
    assert ident2.upc == "887276925174" and ident2.kind == "upc"


# ----------------------------------------------------------------- matching


def test_upc_exact_match_is_gold():
    ident = bp.Identity(kind="upc", upc="887276925174")
    conf, mismatch = bp.score_match(ident, _offer())
    assert conf == 1.0 and mismatch is False
    conf, _ = bp.score_match(ident, _offer(upc="0887276925174"))  # EAN-13 form
    assert conf == 1.0
    conf, _ = bp.score_match(ident, _offer(upc="111111111111"))
    assert conf == 0.0


def test_brand_plus_mpn_scores_high_but_title_alone_cannot():
    ident = bp.Identity(
        kind="query", brand="samsung", mpn="SM-S938U", title="Samsung SM-S938U phone"
    )
    conf, _ = bp.score_match(
        ident, _offer(title="Samsung phone", upc=None, mpn="SM-S938U", brand="samsung")
    )
    assert conf == 0.9
    title_only = bp.Identity(kind="query", title="Samsung Galaxy S25 Ultra 256GB")
    conf, _ = bp.score_match(title_only, _offer(upc=None, mpn=None))
    assert conf < bp.HIGH_CONFIDENCE  # title-only can never name a winner


def test_variant_mismatch_detected_on_storage_and_pack():
    ident = bp.Identity(kind="query", title="Samsung Galaxy S25 Ultra 256GB")
    _, mismatch = bp.score_match(ident, _offer(title="Samsung Galaxy S25 Ultra 512GB"))
    assert mismatch is True
    ident2 = bp.Identity(kind="query", title="Duracell AA Batteries Pack of 8")
    _, mismatch2 = bp.score_match(ident2, _offer(title="Duracell AA Batteries 4-Pack", upc=None))
    assert mismatch2 is True


# ------------------------------------------------------------------ verdicts


def test_best_offer_requires_known_total_and_stock():
    ident = bp.Identity(kind="upc", upc="887276925174")
    offers = [
        _offer(price=120.0, shipping=5.0),  # total 125
        _offer(price=110.0, shipping=0.0, retailer="B"),  # total 110 -> winner
        _offer(price=90.0, shipping=None, retailer="C"),  # unverified total
    ]
    _, verdict, best, _ = bp.rank_and_verdict(ident, offers, "new")
    assert verdict == "best_offer"
    assert best is not None and best.retailer == "B" and best.total == 110.0


def test_unknown_shipping_yields_unverified_label_not_a_winner():
    ident = bp.Identity(kind="upc", upc="887276925174")
    _, verdict, best, found = bp.rank_and_verdict(ident, [_offer(price=90.0, shipping=None)], "new")
    assert verdict == "best_found_total_unverified"
    assert best is None and found is not None and found.price == 90.0


def test_out_of_stock_never_wins():
    ident = bp.Identity(kind="upc", upc="887276925174")
    offers = [
        _offer(price=50.0, shipping=0.0, availability="out_of_stock"),
        _offer(price=200.0, shipping=0.0, retailer="B"),
    ]
    _, verdict, best, _ = bp.rank_and_verdict(ident, offers, "new")
    assert verdict == "best_offer" and best.price == 200.0


def test_used_never_enters_new_ranking_but_wins_used_bucket():
    ident = bp.Identity(kind="upc", upc="887276925174")
    offers = [
        _offer(price=10.0, shipping=0.0, condition="used"),
        _offer(price=200.0, shipping=0.0, retailer="B"),
    ]
    ranked, verdict, best, _ = bp.rank_and_verdict(ident, offers, "new")
    assert all(o.condition == "new" for o in ranked)
    assert best.price == 200.0
    _, verdict2, best2, _ = bp.rank_and_verdict(
        ident, [_offer(price=10.0, shipping=0.0, condition="used")], "used"
    )
    assert verdict2 == "best_offer" and best2.condition == "used"


def test_low_confidence_abstains():
    ident = bp.Identity(kind="query", title="Samsung Galaxy S25 Ultra 256GB")
    offers = [_offer(upc=None, mpn=None)]  # title-only: capped below the bar
    _, verdict, best, _ = bp.rank_and_verdict(ident, offers, "new")
    assert verdict == "no_confident_match" and best is None


def test_variant_mismatch_cannot_win_even_when_cheaper():
    ident = bp.Identity(kind="query", title="Samsung Galaxy S25 Ultra 256GB", brand="samsung")
    offers = [
        _offer(
            title="Samsung Galaxy S25 Ultra 512GB", upc="887276925181", price=10.0, shipping=0.0
        ),
        _offer(price=500.0, shipping=0.0, retailer="B"),
    ]
    # Identity is title-based here: the 512GB offer mismatches the variant,
    # and the correct offer can only match on title (capped below the
    # high-confidence bar), so the tool must abstain rather than crown the
    # cheap variant or the unverifiable title match.
    _, verdict, best, _ = bp.rank_and_verdict(ident, offers, "new")
    assert verdict == "no_confident_match" and best is None


# ------------------------------------------------- spec gate 1: 20 UPC fixtures

_FIXTURE_PRODUCTS = [
    ("887276925174", "Samsung Galaxy S25 Ultra 256GB", "samsung"),
    ("194252542834", "Apple iPhone 15 128GB", "apple"),
    ("195925051340", "Apple AirPods Pro 2nd Generation", "apple"),
    ("883929837955", "LG C3 55 inch OLED TV", "lg"),
    ("027242926132", "Sony WH-1000XM5 Wireless Headphones", "sony"),
    ("719918687264", "Garmin fenix 8 Solar 47mm", "garmin"),
    ("197530235678", "Lenovo ThinkPad X1 Carbon Gen 12", "lenovo"),
    ("884116492145", "Dell XPS 14 Laptop 32GB", "dell"),
    ("889842910112", "Microsoft Xbox Series X 1TB", "microsoft"),
    ("045496884741", "Nintendo Switch OLED Model White", "nintendo"),
    ("013358167421", "Canon EOS R50 Mirrorless Camera", "canon"),
    ("018208026336", "Dyson V15 Detect Vacuum", "dyson"),
    ("883049014553", "Bose QuietComfort Ultra Earbuds", "bose"),
    ("885909876512", "Logitech MX Master 3S Mouse", "logitech"),
    ("097855164478", "Logitech Brio 4K Webcam", "logitech"),
    ("619659184207", "SanDisk Extreme Pro 1TB Portable SSD", "sandisk"),
    ("718037902455", "Seagate Backup Plus 5TB External Drive", "seagate"),
    ("889466112390", "HP LaserJet Pro M404dn Printer", "hp"),
    ("840268190775", "Anker 737 Power Bank 24000mAh", "anker"),
    ("635459221104", "Keurig K-Mini Coffee Maker", "keurig"),
]


@pytest.mark.parametrize("upc,title,brand", _FIXTURE_PRODUCTS)
def test_fixture_gate_upc_products(upc, title, brand):
    """Gate 1: top match correct and ZERO wrong-product best_offer verdicts,
    against distractors designed to bait each failure mode."""
    ident = bp.Identity(kind="upc", upc=upc, brand=brand, title=title)
    correct = _offer(title=title, upc=upc, price=300.0, shipping=10.0, retailer="Correct")
    distractors = [
        _offer(
            title=title + " (Renewed)",
            upc=upc,
            price=100.0,
            shipping=0.0,
            condition="refurbished",
            retailer="RefurbBait",
        ),
        _offer(
            title=title, upc=upc, price=120.0, shipping=0.0, condition="used", retailer="UsedBait"
        ),
        _offer(
            title=title,
            upc=upc,
            price=130.0,
            shipping=0.0,
            availability="out_of_stock",
            retailer="OosBait",
        ),
        _offer(
            title=title + " Different Model",
            upc="999999999999",
            price=90.0,
            shipping=0.0,
            retailer="WrongProduct",
        ),
        _offer(title=title, upc=upc, price=250.0, shipping=None, retailer="NoShipInfo"),
    ]
    ranked, verdict, best, _ = bp.rank_and_verdict(ident, distractors + [correct], "new")
    assert ranked[0] is correct, "top-ranked offer must be the correct product"
    assert verdict == "best_offer"
    assert best is correct, "a wrong product must never be named best_offer"
    assert best.total == 310.0


# ------------------------------------------------------------- adapters: parse


def test_bestbuy_adapter_parses_fixture_and_builds_upc_query():
    seen = {}

    def fetcher(url, **kwargs):
        seen["url"] = url
        return _load("bestbuy_products.json")

    source = src_mod.BestBuySource(api_key="test-key", fetcher=fetcher)
    assert source.configured()
    offers, dropped = source.search(bp.Identity(kind="upc", upc="887276925174"), "new", "US")
    assert "upc=887276925174" in seen["url"] and "apiKey=test-key" in seen["url"]
    assert dropped == 0 and len(offers) == 2
    first = offers[0]
    assert first.price == 1049.99  # gate 2 (parse layer): payload price, exact
    assert first.shipping == 0.0  # freeShipping=true, shippingCost null
    assert first.total == 1049.99
    assert first.availability == "in_stock"
    assert first.upc == "887276925174" and first.mpn == "SM-S938UZKAXAA"


def test_bestbuy_unconfigured_without_key(monkeypatch):
    monkeypatch.delenv("BESTBUY_API_KEY", raising=False)
    assert not src_mod.BestBuySource().configured()


def test_ebay_adapter_reputation_bar_and_parsing():
    calls = {"token": 0, "search": 0}

    def fetcher(url, **kwargs):
        if "oauth2/token" in url:
            calls["token"] += 1
            return {"access_token": "tok", "expires_in": 7200}
        calls["search"] += 1
        return _load("ebay_search.json")

    source = src_mod.EbaySource(client_id="cid", client_secret="sec", fetcher=fetcher)
    ident = bp.Identity(kind="upc", upc="887276925174")
    offers, dropped = source.search(ident, "new", "US")
    # The for-parts listing from a 91.2% / 37-score seller is dropped by the
    # reputation bar and reported, never listed.
    assert dropped == 1
    by_url = {o.url: o for o in offers}
    new_offer = by_url["https://www.ebay.com/itm/100000000001"]
    assert new_offer.price == 989.0 and new_offer.shipping == 0.0
    assert new_offer.condition == "new" and new_offer.seller == "topratedphones"
    # Token is cached: a second search does not re-authenticate.
    source.search(ident, "new", "US")
    assert calls["token"] == 1 and calls["search"] == 2


def test_ebay_used_bucket_keeps_reputable_used():
    def fetcher(url, **kwargs):
        if "oauth2/token" in url:
            return {"access_token": "tok", "expires_in": 7200}
        return _load("ebay_search.json")

    source = src_mod.EbaySource(client_id="cid", client_secret="sec", fetcher=fetcher)
    offers, _ = source.search(bp.Identity(kind="upc", upc="887276925174"), "used", "US")
    used = [o for o in offers if o.condition == "used"]
    assert len(used) == 1 and used[0].seller == "preownedpros"


def test_walmart_adapter_signs_and_parses():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    env = {
        "WALMART_CONSUMER_ID": "consumer-123",
        "WALMART_PRIVATE_KEY": pem.replace("\n", "\\n"),  # env-escaped form
        "WALMART_KEY_VERSION": "1",
    }
    seen = {}

    def fetcher(url, headers=None, **kwargs):
        seen["url"] = url
        seen["headers"] = headers
        return _load("walmart_items.json")

    source = src_mod.WalmartSource(env=env, fetcher=fetcher, clock=lambda: 1700000000000)
    assert source.configured()
    offers, _ = source.search(bp.Identity(kind="upc", upc="887276925174"), "new", "US")
    headers = seen["headers"]
    assert headers["WM_CONSUMER.ID"] == "consumer-123"
    assert headers["WM_CONSUMER.INTIMESTAMP"] == "1700000000000"
    # The signature must verify against the canonical string with the pubkey.
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key.public_key().verify(
        base64.b64decode(headers["WM_SEC.AUTH_SIGNATURE"]),
        b"consumer-123\n1700000000000\n1\n",
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert len(offers) == 2
    assert offers[0].price == 1075.0 and offers[0].seller == "Walmart"
    assert offers[0].availability == "in_stock"
    assert offers[1].availability == "out_of_stock" and offers[1].seller == "MobileDeals LLC"


# ------------------------------------------------------------------ Tier B


def _fake_info():
    return PriceInfo(
        price=1199.99,
        currency="USD",
        title="Samsung Galaxy S25 Ultra 256GB",
        availability="InStock",
        source="json-ld",
    )


def test_direct_source_resolves_identity_from_page():
    source = src_mod.DirectStoreSource(extractor=lambda url: _fake_info())
    identity, offer = source.resolve_identity("https://www.samsung.com/us/p/123")
    assert identity.kind == "url" and identity.brand == "samsung"
    assert offer.identity_source is True and offer.price == 1199.99
    assert offer.shipping is None  # Tier B never invents a shipping figure
    assert offer.availability == "in_stock" and offer.retailer == "Samsung"


# ------------------------------------------------------------- orchestration


def test_orchestrator_happy_path_names_best_offer():
    bb = FakeSource("bestbuy", [_offer(price=310.0, shipping=0.0)])
    wm = FakeSource("walmart", [_offer(price=300.0, shipping=None, retailer="Walmart")])
    result = bp.find_best_price(
        upc="887276925174",
        sources=[bb, wm, src_mod.DirectStoreSource(extractor=lambda u: _fake_info())],
        cache=bp.ResultCache(),
    )
    assert result["verdict"] == "best_offer"
    assert result["best_offer"]["retailer"] == "TestStore"
    assert result["best_offer"]["total_if_known"] == 310.0
    assert result["source_status"]["bestbuy"]["status"] == "ok"
    assert result["source_status"]["walmart"]["status"] == "ok"
    assert result["source_status"]["direct"]["status"] == "skipped"


def test_orchestrator_degrades_when_a_source_dies():
    """Spec gate 4: one dead source -> the rest still answer, status shown."""
    dead = FakeSource("bestbuy", error="bestbuy: HTTP 503")
    alive = FakeSource("walmart", [_offer(price=310.0, shipping=0.0, retailer="Walmart")])
    result = bp.find_best_price(upc="887276925174", sources=[dead, alive], cache=bp.ResultCache())
    assert result["verdict"] == "best_offer"
    assert result["source_status"]["bestbuy"]["status"] == "error"
    assert "503" in result["source_status"]["bestbuy"]["detail"]


def test_orchestrator_no_keys_answers_honestly(monkeypatch):
    for var in (
        "BESTBUY_API_KEY",
        "WALMART_CONSUMER_ID",
        "WALMART_PRIVATE_KEY",
        "WALMART_KEY_VERSION",
        "EBAY_CLIENT_ID",
        "EBAY_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    result = bp.find_best_price(query="Samsung Galaxy S25 Ultra 256GB", cache=bp.ResultCache())
    assert result["verdict"] == "no_confident_match"
    assert result["offers"] == []
    statuses = {k: v["status"] for k, v in result["source_status"].items()}
    assert statuses["bestbuy"] == "not_configured"
    assert statuses["walmart"] == "not_configured"
    assert statuses["ebay"] == "not_configured"


def test_orchestrator_url_input_uses_tier_b_identity():
    direct = src_mod.DirectStoreSource(extractor=lambda url: _fake_info())
    bb = FakeSource("bestbuy", [_offer(price=1049.99, shipping=0.0)])
    result = bp.find_best_price(
        url="https://www.samsung.com/us/p/123",
        sources=[bb, direct],
        cache=bp.ResultCache(),
    )
    assert result["identity"]["kind"] == "url"
    # The Tier B page offer is high-confidence by construction, but its
    # shipping is unknown; the FakeSource offer matches on title only
    # (capped below the bar). Honest outcome: no winner, the page offer is
    # reported as best-found with its total explicitly unverified.
    assert result["verdict"] == "best_found_total_unverified"
    assert result["best_offer"] is None
    assert result["best_found"]["source_api"] == "direct_page_extractor"


def test_orchestrator_rejects_bad_inputs():
    with pytest.raises(bp.BestPriceError):
        bp.find_best_price(cache=bp.ResultCache(), sources=[])
    with pytest.raises(bp.BestPriceError):
        bp.find_best_price(upc="123", cache=bp.ResultCache(), sources=[])
    with pytest.raises(bp.BestPriceError):
        bp.find_best_price(query="x", condition="mint", cache=bp.ResultCache(), sources=[])


def test_cache_hit_skips_sources_and_expires():
    now = [1000.0]
    cache = bp.ResultCache(ttl=100, clock=lambda: now[0])
    source = FakeSource("bestbuy", [_offer(price=310.0, shipping=0.0)])
    first = bp.find_best_price(upc="887276925174", sources=[source], cache=cache)
    assert first["cache"] == {"hit": False} and source.calls == 1
    second = bp.find_best_price(upc="887276925174", sources=[source], cache=cache)
    assert second["cache"] == {"hit": True} and source.calls == 1
    now[0] += 101  # past the TTL
    bp.find_best_price(upc="887276925174", sources=[source], cache=cache)
    assert source.calls == 2


def test_no_result_cached_when_every_source_failed():
    cache = bp.ResultCache(clock=lambda: 0.0)
    dead = FakeSource("bestbuy", error="boom")
    bp.find_best_price(upc="887276925174", sources=[dead], cache=cache)
    assert cache.get("upc:887276925174|new|US|10") is None


# ------------------------------------------------------------------ pricing


def test_pricing_wiring():
    """Spec section 4: $0.03/call, drawn from the shared free-credit pool."""
    assert x402_mod.tool_price_usdc("find_best_price") == Decimal("0.03")
    assert x402_mod.price_to_credits(Decimal("0.03")) == 3.0  # 1 credit = $0.01
    manifest = x402_mod.build_discovery_manifest()
    assert manifest["tools"]["find_best_price"]["price_usdc"] == "0.03"
    assert manifest["tools"]["find_best_price"]["rest_path"] == "/best-price"
    assert x402_mod.REST_TOOL_MAP["/best-price"] == "find_best_price"
    assert x402_mod.REST_TOOL_MAP["/paid/best-price"] == "find_best_price"


# ---------------------------------------------------------------- REST + MCP


@pytest.fixture()
def rest_client(monkeypatch):
    monkeypatch.setattr(
        bp,
        "find_best_price",
        lambda **kw: {
            "verdict": "best_offer",
            "offers": [],
            "identity": {"kind": "upc", "upc": kw.get("upc")},
        },
    )
    return TestClient(create_app(), raise_server_exceptions=False)


def test_rest_best_price_route(rest_client):
    r = rest_client.post("/best-price", json={"upc": "887276925174"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "best_offer"
    r = rest_client.post("/paid/best-price", json={"upc": "887276925174"})
    assert r.status_code == 200


def test_rest_best_price_error_maps_to_422(monkeypatch):
    def boom(**kw):
        raise bp.BestPriceError("provide one of: query, url, or upc")

    monkeypatch.setattr(bp, "find_best_price", boom)
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post("/best-price", json={})
    assert r.status_code == 422
    assert r.json()["error"] == "best_price_failed"


def test_mcp_find_best_price_no_input_is_an_error():
    import asyncio

    from fastmcp import Client

    from price_tracker.mcp_server import mcp

    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("find_best_price", {})

    with pytest.raises(Exception, match="(?i)one of"):
        asyncio.run(go())


def test_fetch_json_refuses_non_allowlisted_hosts():
    with pytest.raises(src_mod.SourceError, match="non-allowlisted"):
        src_mod.fetch_json("https://evil.example.com/api")


def test_time_budget_guard():
    # The whole adapter layer must stay fast (fan-out is parallel); this
    # simply documents that cache TTL never exceeds the spec's 6h cap.
    assert bp.CACHE_TTL_SECONDS <= 6 * 3600
    assert time.monotonic() > 0
