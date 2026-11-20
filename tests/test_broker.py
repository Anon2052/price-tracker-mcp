"""Broker layer — products lane tests (broker-layer-spec.md sections 1-3, 5).

Named gates from spec section 5, as they apply to this lane:
- Gate 1 (commission-blind ranking): permuting affiliate rates across
  offers — including zero, and highest-on-the-wrong-offer — must not
  change ranked order or verdict.
- Gate 3 (no buyer markup): a broker-tagged link and the plain direct
  link for the same offer carry identical price, shipping, and total;
  the tagged URL differs only by the program's tracking parameter.
- Gate 4 (disclosure completeness): 100% of broker responses carry
  ``take_disclosure``; 100% of direct responses carry the direct wording
  and contain NO affiliate parameters in any returned URL.
- Gate 5 (path-invariant ranking): same request as direct and as broker
  returns identical ranking, verdict, and per-offer prices; only links
  and disclosure differ.
Gates 2 (delivered-total only), 7 (copy), and the live half of gate 3's
spot-check are inherited / post-key concerns, covered elsewhere or
deferred by the spec itself. Gate 6 is the compute lane.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from price_tracker import bestprice as bp
from price_tracker import broker as br
from price_tracker.rest_app import create_app

PROGRAM_ENV = (
    "BESTBUY_AFFILIATE_TAG",
    "BESTBUY_AFFILIATE_PARAM",
    "BESTBUY_AFFILIATE_RATE",
    "WALMART_AFFILIATE_TAG",
    "WALMART_AFFILIATE_PARAM",
    "WALMART_AFFILIATE_RATE",
    "EBAY_AFFILIATE_TAG",
    "EBAY_AFFILIATE_PARAM",
    "EBAY_AFFILIATE_RATE",
)
API_KEY_ENV = (
    "BESTBUY_API_KEY",
    "WALMART_CONSUMER_ID",
    "WALMART_PRIVATE_KEY",
    "WALMART_KEY_VERSION",
    "EBAY_CLIENT_ID",
    "EBAY_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def _clean_broker_env(monkeypatch):
    for var in PROGRAM_ENV + API_KEY_ENV:
        monkeypatch.delenv(var, raising=False)


# ------------------------------------------------------------------ helpers


def _offer_dict(**over) -> dict:
    base = dict(
        retailer="eBay",
        seller="TopSeller",
        price=90.0,
        currency="USD",
        shipping=0.0,
        total_if_known=90.0,
        availability="in_stock",
        condition="new",
        url="https://www.ebay.com/itm/123",
        source_api="ebay_browse_api",
        fetched_at="2026-11-03T00:00:00Z",
        match_confidence=1.0,
        title="Samsung Galaxy S25 Ultra 256GB",
        variant_mismatch=False,
    )
    base.update(over)
    return base


def _bestbuy_dict(**over) -> dict:
    base = dict(
        retailer="Best Buy",
        seller="Best Buy",
        price=100.0,
        shipping=0.0,
        total_if_known=100.0,
        url="https://www.bestbuy.com/site/galaxy/123.p",
        source_api="bestbuy_products_api",
    )
    base.update(over)
    return _offer_dict(**base)


def _result(offers, *, verdict="best_offer", winner=0, found=False) -> dict:
    """A find_best_price-shaped result dict (as Offer.to_dict produces)."""
    best = dict(offers[winner]) if (offers and verdict == "best_offer") else None
    best_found = dict(offers[winner]) if (offers and found) else None
    return {
        "identity": {
            "kind": "upc",
            "upc": "887276925174",
            "brand": "samsung",
            "mpn": None,
            "title": "Samsung Galaxy S25 Ultra 256GB",
            "source_url": None,
        },
        "condition": "new",
        "country": "US",
        "verdict": verdict,
        "best_offer": best,
        "best_found": best_found,
        "offers": [dict(o) for o in offers],
        "source_status": {},
        "fetched_at": "2026-11-03T00:00:00Z",
        "cache": {"hit": False},
        "notes": [],
    }


def _query_params(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


class FakeSource:
    def __init__(self, name, offers=()):
        self.name = name
        self._offers = list(offers)
        self.calls = 0

    def configured(self):
        return True

    def search(self, identity, condition, country):
        self.calls += 1
        return list(self._offers), 0


def _bp_offer(**over) -> bp.Offer:
    base = dict(
        retailer="eBay",
        seller="TopSeller",
        price=90.0,
        currency="USD",
        shipping=0.0,
        availability="in_stock",
        condition="new",
        url="https://www.ebay.com/itm/123",
        source_api="ebay_browse_api",
        title="Samsung Galaxy S25 Ultra 256GB",
        upc="887276925174",
    )
    base.update(over)
    return bp.Offer(**base)


# ------------------------------------------------------- direct path basics


def test_default_path_is_direct_and_states_both_paths_offered():
    src = FakeSource("ebay", [_bp_offer()])
    result = bp.find_best_price(upc="887276925174", sources=[src], cache=bp.ResultCache())
    assert result["path_chosen"] == "direct"
    assert result["paths_offered"] == ["direct", "broker"]
    assert result["take_disclosure"] == br.DIRECT_DISCLOSURE
    assert result["take_rate_if_known"] is None
    assert result["best_offer"]["affiliate"] is False


def test_direct_links_carry_no_affiliate_parameters(monkeypatch):
    """Gate 4, direct half: with every program enrolled, a direct response
    still contains no affiliate parameter and no tag value in any URL."""
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    monkeypatch.setenv("BESTBUY_AFFILIATE_TAG", "bbtag2")
    monkeypatch.setenv("WALMART_AFFILIATE_TAG", "wmtag3")
    offers = [_offer_dict(), _bestbuy_dict()]
    out = br.apply_path(_result(offers), "direct")
    urls = [o["url"] for o in out["offers"]] + [out["best_offer"]["url"]]
    for url in urls:
        params = _query_params(url)
        assert not ({"ref", "affp1", "mkcid"} & set(params))
        assert "ebaytag1" not in url and "bbtag2" not in url and "wmtag3" not in url
    assert all(o["affiliate"] is False for o in out["offers"])
    assert out["take_disclosure"] == br.DIRECT_DISCLOSURE


def test_apply_path_never_mutates_the_input_result(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    result = _result([_offer_dict()])
    br.apply_path(result, "broker")
    assert "take_disclosure" not in result
    assert "affiliate" not in result["offers"][0]
    assert result["best_offer"]["url"] == "https://www.ebay.com/itm/123"


# ------------------------------------------------------------- broker path


def test_broker_tags_enrolled_winner_and_discloses_verbatim(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    out = br.apply_path(_result([_offer_dict(), _bestbuy_dict()]), "broker")
    assert out["path_chosen"] == "broker"
    assert out["paths_offered"] == ["direct", "broker"]
    assert out["take_disclosure"] == (
        "Broker path: IOConnex may earn a commission from eBay if you buy "
        "through this link. The commission is paid by the retailer — it is "
        "not added to your price. Offers are ranked by your lowest "
        "delivered total, not by commission."
    )
    assert out["take_rate_if_known"] is None  # program publishes none to us
    winner = out["best_offer"]
    assert winner["affiliate"] is True
    assert _query_params(winner["url"])["mkcid"] == "ebaytag1"
    listed = next(o for o in out["offers"] if o["retailer"] == "eBay")
    assert listed["url"] == winner["url"] and listed["affiliate"] is True
    other = next(o for o in out["offers"] if o["retailer"] == "Best Buy")
    assert other["affiliate"] is False and "?" not in other["url"]


def test_broker_discloses_published_rate_when_program_gives_one(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    monkeypatch.setenv("EBAY_AFFILIATE_RATE", "5%")
    out = br.apply_path(_result([_offer_dict()]), "broker")
    assert out["take_rate_if_known"] == "5%"


def test_broker_custom_param_name_from_env(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    monkeypatch.setenv("EBAY_AFFILIATE_PARAM", "campid")
    out = br.apply_path(_result([_offer_dict()]), "broker")
    assert _query_params(out["best_offer"]["url"])["campid"] == "ebaytag1"


def test_api_key_is_not_affiliate_enrollment(monkeypatch):
    """An API-live retailer with no affiliate enrollment stays untagged."""
    monkeypatch.setenv("EBAY_CLIENT_ID", "some-client-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "some-client-secret")
    out = br.apply_path(_result([_offer_dict()]), "broker")
    assert out["best_offer"]["affiliate"] is False
    assert out["best_offer"]["url"] == "https://www.ebay.com/itm/123"
    assert out["take_disclosure"] == br.NO_AFFILIATE_DISCLOSURE
    assert out["take_rate_if_known"] is None


def test_broker_with_no_enrolled_retailer_earns_nothing_disclosed():
    out = br.apply_path(_result([_offer_dict(), _bestbuy_dict()]), "broker")
    assert out["take_disclosure"] == br.NO_AFFILIATE_DISCLOSURE
    assert all(o["affiliate"] is False for o in out["offers"])
    assert all("?" not in o["url"] for o in out["offers"])


def test_broker_winner_not_enrolled_tags_nothing(monkeypatch):
    """Winner at a non-enrolled retailer; a lower offer IS enrolled. The
    ranking stands, nothing is tagged, and the disclosure stays truthful
    (it must not claim no arrangement exists anywhere)."""
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    offers = [_bestbuy_dict(), _offer_dict(price=120.0, total_if_known=120.0)]
    out = br.apply_path(_result(offers, winner=0), "broker")
    assert out["best_offer"]["retailer"] == "Best Buy"
    assert all(o["affiliate"] is False for o in out["offers"])
    assert all("mkcid" not in o["url"] for o in out["offers"])
    assert out["take_disclosure"] == br.WINNER_NOT_ENROLLED_DISCLOSURE


def test_broker_tags_best_found_when_total_unverified(monkeypatch):
    monkeypatch.setenv("WALMART_AFFILIATE_TAG", "wmtag3")
    wm = _offer_dict(
        retailer="Walmart",
        seller="Walmart",
        url="https://www.walmart.com/ip/123",
        source_api="walmart_affiliate_api",
        shipping=None,
        total_if_known=None,
    )
    result = _result([wm], verdict="best_found_total_unverified", winner=0, found=True)
    result["best_offer"] = None
    out = br.apply_path(result, "broker")
    assert out["best_found"]["affiliate"] is True
    assert _query_params(out["best_found"]["url"])["affp1"] == "wmtag3"
    assert "from Walmart" in out["take_disclosure"]


def test_broker_no_winner_tags_nothing(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    result = _result([_offer_dict()], verdict="no_confident_match")
    out = br.apply_path(result, "broker")
    assert out["take_disclosure"] == br.NO_AFFILIATE_DISCLOSURE
    assert all(o["affiliate"] is False for o in out["offers"])


def test_malformed_tag_in_env_falls_back_to_plain_link(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "bad tag!")
    out = br.apply_path(_result([_offer_dict()]), "broker")
    assert out["best_offer"]["affiliate"] is False
    assert out["best_offer"]["url"] == "https://www.ebay.com/itm/123"
    assert out["take_disclosure"]  # still disclosed, never undisclosed


def test_attribution_log_line_has_hash_not_tag_or_link(monkeypatch, caplog):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    with caplog.at_level(logging.INFO, logger="price_tracker.broker"):
        br.apply_path(_result([_offer_dict()]), "broker")
    lines = [r.getMessage() for r in caplog.records if "broker_attribution" in r.getMessage()]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert payload["retailer"] == "eBay"
    assert len(payload["offer_identity_hash"]) == 64
    assert "ebaytag1" not in lines[0] and "ebay.com" not in lines[0]


# ------------------------------------------------------------------ gates


def _ranking_fingerprint(result: dict) -> str:
    return json.dumps(
        {
            "verdict": result["verdict"],
            "order": [(o["retailer"], o["price"], o["total_if_known"]) for o in result["offers"]],
            "winner": result["best_offer"]["retailer"] if result["best_offer"] else None,
        },
        sort_keys=True,
    )


def test_gate1_commission_blind_ranking(monkeypatch):
    """Permute published rates (zero, high-on-the-loser, high-on-the-winner):
    ranked order, verdict, and the winning link must not move."""
    offers = [_offer_dict(), _bestbuy_dict()]  # eBay $90 wins, Best Buy $100
    fingerprints, winner_urls = set(), set()
    for ebay_rate, bb_rate in (("0%", "50%"), ("50%", "0%"), ("0%", "0%"), ("99%", "1%")):
        monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
        monkeypatch.setenv("BESTBUY_AFFILIATE_TAG", "bbtag2")
        monkeypatch.setenv("EBAY_AFFILIATE_RATE", ebay_rate)
        monkeypatch.setenv("BESTBUY_AFFILIATE_RATE", bb_rate)
        out = br.apply_path(_result(offers), "broker")
        fingerprints.add(_ranking_fingerprint(out))
        winner_urls.add(out["best_offer"]["url"])
        # Even at a 0% winner rate / 50% loser rate, the winner — and only
        # the winner — is tagged; the loser is never promoted or tagged.
        loser = next(o for o in out["offers"] if o["retailer"] == "Best Buy")
        assert loser["affiliate"] is False and "ref=" not in loser["url"]
    assert len(fingerprints) == 1
    assert len(winner_urls) == 1


def test_gate3_tagging_adds_no_buyer_markup(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    result = _result([_offer_dict(), _bestbuy_dict()])
    direct = br.apply_path(result, "direct")
    broker = br.apply_path(result, "broker")
    for d, b in zip(direct["offers"], broker["offers"], strict=True):
        assert d["price"] == b["price"]
        assert d["shipping"] == b["shipping"]
        assert d["total_if_known"] == b["total_if_known"]
    d_url = urllib.parse.urlparse(direct["best_offer"]["url"])
    b_url = urllib.parse.urlparse(broker["best_offer"]["url"])
    assert (d_url.scheme, d_url.netloc, d_url.path) == (b_url.scheme, b_url.netloc, b_url.path)
    assert set(_query_params(broker["best_offer"]["url"])) - set(
        _query_params(direct["best_offer"]["url"])
    ) == {"mkcid"}


def test_gate4_every_broker_response_carries_disclosure(monkeypatch):
    scenarios = [
        _result([_offer_dict()]),
        _result([_offer_dict(), _bestbuy_dict()]),
        _result([_offer_dict()], verdict="no_confident_match"),
        _result([], verdict="no_confident_match"),
    ]
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    scenarios.append(_result([_offer_dict()]))
    for result in scenarios:
        out = br.apply_path(result, "broker")
        assert out["take_disclosure"]
        assert out["path_chosen"] == "broker"
        assert out["paths_offered"] == ["direct", "broker"]
        assert "take_rate_if_known" in out


def test_gate5_path_invariant_ranking_through_orchestrator(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    offers = [
        _bp_offer(),
        _bp_offer(
            retailer="Best Buy",
            seller="Best Buy",
            price=100.0,
            url="https://www.bestbuy.com/site/galaxy/123.p",
            source_api="bestbuy_products_api",
        ),
    ]

    def run(path):
        src = FakeSource("mixed", offers)
        return bp.find_best_price(
            upc="887276925174", sources=[src], cache=bp.ResultCache(), path=path
        )

    direct, broker = run("direct"), run("broker")
    assert _ranking_fingerprint(direct) == _ranking_fingerprint(broker)
    assert direct["best_offer"]["price"] == broker["best_offer"]["price"]
    assert direct["best_offer"]["url"] != broker["best_offer"]["url"]  # tagged
    assert direct["take_disclosure"] != broker["take_disclosure"]


def test_direct_and_broker_share_one_cached_fanout(monkeypatch):
    monkeypatch.setenv("EBAY_AFFILIATE_TAG", "ebaytag1")
    src = FakeSource("ebay", [_bp_offer()])
    cache = bp.ResultCache()
    first = bp.find_best_price(upc="887276925174", sources=[src], cache=cache, path="direct")
    second = bp.find_best_price(upc="887276925174", sources=[src], cache=cache, path="broker")
    assert src.calls == 1
    assert first["cache"] == {"hit": False} and second["cache"] == {"hit": True}
    assert second["path_chosen"] == "broker"
    assert second["best_offer"]["affiliate"] is True
    stored = cache.get("upc:887276925174|new|US|10")
    assert "take_disclosure" not in stored  # cache stays path-neutral
    assert "affiliate" not in stored["offers"][0]


# ------------------------------------------------------------- validation


def test_invalid_path_rejected_by_orchestrator():
    with pytest.raises(bp.BestPriceError, match="path"):
        bp.find_best_price(upc="887276925174", sources=[], cache=bp.ResultCache(), path="sideways")


def test_rest_broker_path_end_to_end(monkeypatch):
    for var in API_KEY_ENV:
        monkeypatch.delenv(var, raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post("/best-price", json={"upc": "887276925174", "path": "broker"})
    assert r.status_code == 200
    body = r.json()
    assert body["path_chosen"] == "broker"
    assert body["take_disclosure"] == br.NO_AFFILIATE_DISCLOSURE
    r = client.post("/paid/best-price", json={"upc": "887276925174"})
    assert r.status_code == 200
    assert r.json()["path_chosen"] == "direct"  # default when omitted


def test_rest_invalid_path_maps_to_422():
    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.post("/best-price", json={"upc": "887276925174", "path": "sideways"})
    assert r.status_code == 422
    assert r.json()["error"] == "best_price_failed"


def test_mcp_tool_accepts_path_and_defaults_to_direct():
    import asyncio

    from fastmcp import Client

    from price_tracker.mcp_server import mcp

    async def go(args):
        async with Client(mcp) as client:
            return await client.call_tool("find_best_price", args)

    broker = asyncio.run(go({"upc": "887276925174", "path": "broker"})).data
    assert broker["path_chosen"] == "broker"
    assert broker["take_disclosure"] == br.NO_AFFILIATE_DISCLOSURE
    direct = asyncio.run(go({"upc": "887276925174"})).data
    assert direct["path_chosen"] == "direct"
    assert direct["take_disclosure"] == br.DIRECT_DISCLOSURE
