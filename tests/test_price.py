"""Tests for price extraction: amount parsing, currency detection, HTML sources."""

from __future__ import annotations

import pytest

from price_tracker.price import (
    PriceError,
    _detect_currency,
    _parse_amount,
    check_price,
    extract_price,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,299.99", 1299.99),
        ("1,299.99", 1299.99),
        ("€1.299,99", 1299.99),  # European decimal comma
        ("£49.99", 49.99),
        ("29,99", 29.99),  # two digits after comma -> decimals
        ("1,299", 1299.0),  # three digits -> thousands separator
        ("1299", 1299.0),
        ("$0.99", 0.99),
        ("  $ 2,499.00  ", 2499.0),
        ("not a price", None),
        ("", None),
        ("-", None),
    ],
)
def test_parse_amount(raw, expected):
    result = _parse_amount(raw)
    assert result == pytest.approx(expected) if expected is not None else result is None


@pytest.mark.parametrize(
    ("raw", "hint", "expected"),
    [
        ("$49.99", None, "USD"),
        ("€49.99", None, "EUR"),
        ("£49.99", None, "GBP"),
        ("49.99", "eur", "EUR"),
        ("49.99", "JPY", "JPY"),
        ("49.99 USD", None, "USD"),
        ("49.99", None, "USD"),  # default
    ],
)
def test_detect_currency(raw, hint, expected):
    assert _detect_currency(raw, hint) == expected


_OG_HTML = """<html><head>
<title>Widget Pro</title>
<meta property="og:price:amount" content="79.99" />
<meta property="og:price:currency" content="USD" />
</head><body><h1>Widget Pro</h1></body></html>"""

_JSONLD_HTML = """<html><head><title>Gadget X</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Gadget X",
 "offers": {"@type": "Offer", "price": "149.50", "priceCurrency": "EUR",
             "availability": "https://schema.org/InStock"}}
</script></head><body></body></html>"""

_SELECTOR_HTML = """<html><head><title>Thing</title></head>
<body><span class="price">$19.95</span></body></html>"""

_NO_PRICE_HTML = """<html><head><title>Nothing here</title></head>
<body><p>Just some text, no price.</p></body></html>"""


def test_extract_price_open_graph():
    info = extract_price(_OG_HTML)
    assert info.price == pytest.approx(79.99)
    assert info.currency == "USD"
    assert info.title == "Widget Pro"
    assert info.source == "open-graph"


def test_extract_price_json_ld():
    info = extract_price(_JSONLD_HTML)
    assert info.price == pytest.approx(149.50)
    assert info.currency == "EUR"
    assert info.title == "Gadget X"
    assert info.availability == "InStock"
    assert info.source == "json-ld"


def test_extract_price_selectors():
    info = extract_price(_SELECTOR_HTML)
    assert info.price == pytest.approx(19.95)
    assert info.currency == "USD"
    assert info.source == "selectors"


def test_extract_price_none_found():
    with pytest.raises(PriceError, match="no price found"):
        extract_price(_NO_PRICE_HTML)


def test_check_price_uses_fetcher(monkeypatch):
    from price_tracker import price as price_mod

    monkeypatch.setattr(price_mod, "fetch_html", lambda url, timeout=20: _OG_HTML)
    info = check_price("https://example.com/product")
    assert info.price == pytest.approx(79.99)


def test_check_price_fetch_error_becomes_price_error(monkeypatch):
    from price_tracker import fetch as fetch_mod
    from price_tracker import price as price_mod

    def boom(url, timeout=20):
        raise fetch_mod.FetchError("URL: only http(s) URLs are allowed")

    monkeypatch.setattr(price_mod, "fetch_html", boom)
    with pytest.raises(PriceError, match="only http"):
        check_price("ftp://example.com/x")
