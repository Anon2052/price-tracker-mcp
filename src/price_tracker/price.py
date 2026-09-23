"""check_price: extract a product's current price from its page.

Extraction sources, in priority order:
1. Open Graph meta tags (og:price:amount / og:price:currency)
2. JSON-LD structured data (offers.price / offers.priceCurrency)
3. Microdata / common selectors ([itemprop=price], .price, #priceblock_ourprice, ...)

Pages are fetched through the SSRF-guarded fetcher shared with the other
connectors, so private/loopback/metadata hosts are rejected.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .fetch import FetchError, fetch_html

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}


class PriceError(Exception):
    """Raised when no price can be extracted from a page."""


@dataclass
class PriceInfo:
    price: float
    currency: str
    title: str | None
    availability: str | None
    source: str  # which extraction method found the price


def _parse_amount(raw: str) -> float | None:
    """Parse a human price string into a float, tolerating separators."""
    s = raw.strip()
    # Drop anything that isn't a digit, comma, dot, or minus.
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s == "-" or not re.search(r"\d", s):
        return None
    negative = s.startswith("-")
    s = s.lstrip("-")
    # When both separators appear, the last one is the decimal separator.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # "1,299" -> 1299 ; "29,99" -> 29.99 (two digits => decimals)
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:
            s = parts[0].replace(",", "") + "." + parts[1]
        else:
            s = s.replace(",", "")
    try:
        value = float(s)
    except ValueError:
        return None
    return -value if negative else value


def _detect_currency(raw: str, hint: str | None = None) -> str:
    if hint:
        hint = hint.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", hint):
            return hint
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in raw:
            return code
    m = re.search(r"\b([A-Z]{3})\b", raw)
    return m.group(1) if m else "USD"


def _from_open_graph(soup: BeautifulSoup) -> PriceInfo | None:
    amount = soup.find("meta", property="og:price:amount")
    if not amount or not amount.get("content"):
        return None
    value = _parse_amount(amount["content"])
    if value is None:
        return None
    currency_tag = soup.find("meta", property="og:price:currency")
    currency = _detect_currency(
        amount["content"], currency_tag.get("content") if currency_tag else None
    )
    return PriceInfo(
        price=value,
        currency=currency,
        title=_page_title(soup),
        availability=None,
        source="open-graph",
    )


def _iter_json_ld_offers(soup: BeautifulSoup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict):
                yield node


def _from_json_ld(soup: BeautifulSoup) -> PriceInfo | None:
    for node in _iter_json_ld_offers(soup):
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            continue
        raw_price = offers.get("price")
        if raw_price is None:
            continue
        value = _parse_amount(str(raw_price))
        if value is None:
            continue
        availability = offers.get("availability")
        if isinstance(availability, str) and "/" in availability:
            availability = availability.rsplit("/", 1)[-1]
        name = node.get("name") if isinstance(node.get("name"), str) else None
        return PriceInfo(
            price=value,
            currency=_detect_currency(str(raw_price), offers.get("priceCurrency")),
            title=name or _page_title(soup),
            availability=availability if isinstance(availability, str) else None,
            source="json-ld",
        )
    return None


_PRICE_SELECTORS = [
    '[itemprop="price"]',
    ".price",
    "#price",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    ".product-price",
    "[data-price]",
]


def _from_selectors(soup: BeautifulSoup) -> PriceInfo | None:
    for selector in _PRICE_SELECTORS:
        el = soup.select_one(selector)
        if el is None:
            continue
        raw = el.get("content") or el.get("data-price") or el.get_text()
        if not raw or not raw.strip():
            continue
        value = _parse_amount(raw)
        if value is None:
            continue
        return PriceInfo(
            price=value,
            currency=_detect_currency(raw),
            title=_page_title(soup),
            availability=None,
            source="selectors",
        )
    return None


def _page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        return og_title["content"].strip()
    return None


def extract_price(html: str) -> PriceInfo:
    """Extract a price from raw HTML. Raises PriceError when none is found."""
    soup = BeautifulSoup(html, "html.parser")
    for method in (_from_open_graph, _from_json_ld, _from_selectors):
        info = method(soup)
        if info is not None:
            return info
    raise PriceError("no price found on page (tried Open Graph, JSON-LD, selectors)")


def check_price(url: str, timeout: int = 20) -> PriceInfo:
    """Fetch a product page (SSRF-guarded) and extract its current price."""
    try:
        html = fetch_html(url, timeout=timeout)
    except FetchError as exc:
        raise PriceError(str(exc)) from exc
    return extract_price(html)
