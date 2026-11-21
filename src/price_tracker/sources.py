"""Tier A + Tier B source adapters for find_best_price (Price Tracker v2).

Tier A — official retailer APIs only, no scraping (spec section 2):
- Best Buy Products API (free key, BESTBUY_API_KEY)
- Walmart Affiliate API (walmart.io; RSA-signed, WALMART_* env)
- eBay Browse API (OAuth client credentials, EBAY_* env)

Each source ships dark: with its key unset it reports ``not_configured``
and the tool answers from the remaining sources (spec gate 4). No source
may be claimed in copy until its key is verified live (spec section 6).

Tier B — the v1 extractor (already built): direct-store / manufacturer
pages with clean structured data, used for URL inputs.

Tier C (licensed aggregator) is OFF in v2.0 by spec and has no code here.

All outbound calls go to a fixed allowlist of official API hosts — never
to caller-supplied URLs (the Tier B page fetch reuses the SSRF-guarded
fetcher behind check_price instead).
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse

import httpx

from .bestprice import Identity, Offer
from .price import PriceError, check_price

MAX_API_BYTES = 5 * 1024 * 1024
API_HOSTS = frozenset({"api.bestbuy.com", "developer.api.walmart.com", "api.ebay.com"})

# eBay reputation bar, frozen at build (spec section 2): a marketplace
# listing only counts as reputable when the seller is Top Rated (eBay's
# own signal) or clears BOTH feedback bars. Everything else is dropped by
# the adapter and counted in filtered_out — never listed, never ranked.
EBAY_MIN_FEEDBACK_PERCENT = 98.0
EBAY_MIN_FEEDBACK_SCORE = 1000

_EBAY_CONDITION_IDS = {
    "new": "1000",
    "refurbished": "2000|2500",
    "used": "3000",
    "any": "1000|1500|2000|2500|3000",
}


class SourceError(Exception):
    """Raised when a source call fails; the orchestrator degrades around it."""


def fetch_json(
    url: str,
    *,
    headers: dict | None = None,
    method: str = "GET",
    data: dict | None = None,
    json_body: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    """Small JSON-over-HTTPS helper restricted to official API hosts."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in API_HOSTS:
        raise SourceError(f"refusing call to non-allowlisted host {host!r}")
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.request(method, url, headers=headers or {}, data=data, json=json_body)
    except httpx.HTTPError as exc:
        raise SourceError(f"{host}: request failed ({exc})") from exc
    if len(resp.content) > MAX_API_BYTES:
        raise SourceError(f"{host}: response exceeds {MAX_API_BYTES} bytes")
    if resp.status_code != 200:
        raise SourceError(f"{host}: HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SourceError(f"{host}: response was not JSON") from exc
    if not isinstance(payload, dict):
        raise SourceError(f"{host}: unexpected JSON shape")
    return payload


def _money(value) -> float | None:  # noqa: ANN001, ANN202
    try:
        if value is None or value == "":
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _condition_class(text: str | None, default: str = "new") -> str:
    t = (text or "").lower()
    if "for parts" in t or "not working" in t:
        return "used"  # never let a parts/repair listing pass as new

    if "certified refurb" in t or "seller refurb" in t or "refurb" in t or "renewed" in t:
        return "refurbished"
    if "open box" in t or "open-box" in t:
        return "open_box"
    if "used" in t or "pre-owned" in t or "preowned" in t:
        return "used"
    if "new" in t:
        return "new"
    return default


# ---------------------------------------------------------------------------
# Best Buy Products API


class BestBuySource:
    name = "bestbuy"
    api_label = "bestbuy_products_api"
    _SHOW = (
        "name,upc,manufacturer,modelNumber,salePrice,regularPrice,"
        "onlineAvailability,freeShipping,shippingCost,url"
    )

    def __init__(self, api_key: str | None = None, fetcher=None) -> None:  # noqa: ANN001
        self._api_key = api_key
        self._fetcher = fetcher or fetch_json

    def configured(self) -> bool:
        return bool(self._key())

    def _key(self) -> str:
        return (self._api_key or os.environ.get("BESTBUY_API_KEY", "")).strip()

    def search(self, identity: Identity, condition: str, country: str):
        key = self._key()
        if identity.upc:
            criteria = f"(upc={identity.upc})"
        else:
            terms = (identity.title or "").split()
            if not terms:
                raise SourceError("bestbuy: no UPC or search terms")
            criteria = "(" + "&".join(f"search={urllib.parse.quote(t)}" for t in terms[:6]) + ")"
        url = (
            f"https://api.bestbuy.com/v1/products{criteria}"
            f"?apiKey={urllib.parse.quote(key)}&format=json&pageSize=10"
            f"&show={self._SHOW}"
        )
        payload = self._fetcher(url)
        offers = []
        for item in payload.get("products") or []:
            price = _money(item.get("salePrice")) or _money(item.get("regularPrice"))
            if price is None or not item.get("url"):
                continue
            shipping = _money(item.get("shippingCost"))
            if shipping is None and item.get("freeShipping") is True:
                shipping = 0.0
            available = item.get("onlineAvailability")
            offers.append(
                Offer(
                    retailer="Best Buy",
                    seller="Best Buy",
                    price=price,
                    currency="USD",
                    shipping=shipping,
                    availability="in_stock"
                    if available
                    else ("out_of_stock" if available is False else "unknown"),
                    condition="new",
                    url=item["url"],
                    source_api=self.api_label,
                    title=item.get("name") or "",
                    upc=str(item["upc"]) if item.get("upc") else None,
                    mpn=item.get("modelNumber"),
                    brand=(item.get("manufacturer") or "").lower() or None,
                    queried_by_upc=bool(identity.upc),
                )
            )
        return offers, 0


# ---------------------------------------------------------------------------
# Walmart Affiliate API (walmart.io, RSA-signed)


def walmart_signature(
    consumer_id: str, key_version: str, timestamp_ms: int, private_key_pem: str
) -> str:
    """WM_SEC.AUTH_SIGNATURE: RSA-SHA256 over id\\ntimestamp\\nversion\\n."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    pem = private_key_pem.replace("\\n", "\n").strip()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    canonical = f"{consumer_id}\n{timestamp_ms}\n{key_version}\n".encode()
    signature = key.sign(canonical, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


class WalmartSource:
    name = "walmart"
    api_label = "walmart_affiliate_api"
    _BASE = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2"

    def __init__(self, env: dict | None = None, fetcher=None, clock=None) -> None:  # noqa: ANN001
        self._env = env
        self._fetcher = fetcher or fetch_json
        self._clock = clock or (lambda: int(time.time() * 1000))

    def _setting(self, name: str) -> str:
        source = self._env if self._env is not None else os.environ
        return (source.get(name, "") or "").strip()

    def configured(self) -> bool:
        return all(
            self._setting(n)
            for n in ("WALMART_CONSUMER_ID", "WALMART_PRIVATE_KEY", "WALMART_KEY_VERSION")
        )

    def _headers(self) -> dict:
        consumer_id = self._setting("WALMART_CONSUMER_ID")
        key_version = self._setting("WALMART_KEY_VERSION")
        timestamp = self._clock()
        try:
            signature = walmart_signature(
                consumer_id, key_version, timestamp, self._setting("WALMART_PRIVATE_KEY")
            )
        except Exception as exc:
            raise SourceError(f"walmart: could not sign request ({exc})") from exc
        return {
            "WM_CONSUMER.ID": consumer_id,
            "WM_CONSUMER.INTIMESTAMP": str(timestamp),
            "WM_SEC.KEY_VERSION": key_version,
            "WM_SEC.AUTH_SIGNATURE": signature,
            "Accept": "application/json",
        }

    def search(self, identity: Identity, condition: str, country: str):
        if identity.upc:
            url = f"{self._BASE}/items?upc={identity.upc}"
        elif identity.title:
            url = f"{self._BASE}/search?query={urllib.parse.quote(identity.title)}"
        else:
            raise SourceError("walmart: no UPC or search terms")
        payload = self._fetcher(url, headers=self._headers())
        items = payload.get("items") or []
        if isinstance(items, dict):  # single-item lookup shape
            items = [items]
        offers = []
        for item in items:
            price = _money(item.get("salePrice")) or _money(item.get("msrp"))
            if price is None or not item.get("productUrl"):
                continue
            stock = (item.get("stock") or "").lower()
            availability = (
                "in_stock"
                if "available" in stock or "limited" in stock
                else "out_of_stock"
                if "out of stock" in stock or "not available" in stock
                else "unknown"
            )
            seller = item.get("sellerName") or ("Walmart" if not item.get("marketplace") else None)
            offers.append(
                Offer(
                    retailer="Walmart",
                    seller=seller,
                    price=price,
                    currency="USD",
                    shipping=_money(item.get("standardShipRate")),
                    availability=availability,
                    condition="new",
                    url=item["productUrl"],
                    source_api=self.api_label,
                    title=item.get("name") or "",
                    upc=str(item["upc"]) if item.get("upc") else None,
                    mpn=None,
                    brand=(item.get("brandName") or "").lower() or None,
                    queried_by_upc=bool(identity.upc),
                )
            )
        return offers, 0


# ---------------------------------------------------------------------------
# eBay Browse API (OAuth client credentials)


class EbaySource:
    name = "ebay"
    api_label = "ebay_browse_api"
    _TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    _SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, client_id=None, client_secret=None, fetcher=None, clock=None):  # noqa: ANN001
        self._client_id = client_id
        self._client_secret = client_secret
        self._fetcher = fetcher or fetch_json
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires = 0.0

    def _credentials(self) -> tuple[str, str]:
        cid = (self._client_id or os.environ.get("EBAY_CLIENT_ID", "")).strip()
        secret = (self._client_secret or os.environ.get("EBAY_CLIENT_SECRET", "")).strip()
        return cid, secret

    def configured(self) -> bool:
        cid, secret = self._credentials()
        return bool(cid and secret)

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires - 30:
            return self._token
        cid, secret = self._credentials()
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        payload = self._fetcher(
            self._TOKEN_URL,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
        )
        token = payload.get("access_token")
        if not token:
            raise SourceError("ebay: OAuth response carried no access_token")
        self._token = token
        self._token_expires = self._clock() + float(payload.get("expires_in") or 7200)
        return token

    def search(self, identity: Identity, condition: str, country: str):
        terms = identity.upc or identity.title or ""
        if not terms:
            raise SourceError("ebay: no UPC or search terms")
        filters = (
            "buyingOptions:{FIXED_PRICE},itemLocationCountry:US,"
            f"conditionIds:{{{_EBAY_CONDITION_IDS[condition]}}}"
        )
        url = (
            f"{self._SEARCH_URL}?q={urllib.parse.quote(terms)}"
            f"&filter={urllib.parse.quote(filters, safe=':,{}|')}&limit=20"
        )
        payload = self._fetcher(url, headers={"Authorization": f"Bearer {self._access_token()}"})
        offers, dropped = [], 0
        for item in payload.get("itemSummaries") or []:
            seller_info = item.get("seller") or {}
            top_rated = item.get("topRatedBuyingExperience") is True
            feedback_pct = _money(seller_info.get("feedbackPercentage")) or 0.0
            feedback_score = seller_info.get("feedbackScore") or 0
            if not top_rated and not (
                feedback_pct >= EBAY_MIN_FEEDBACK_PERCENT
                and feedback_score >= EBAY_MIN_FEEDBACK_SCORE
            ):
                dropped += 1  # reputation bar: not reputable, never listed
                continue
            price_info = item.get("price") or {}
            price = _money(price_info.get("value"))
            if price is None or not item.get("itemWebUrl"):
                continue
            shipping = None
            for option in item.get("shippingOptions") or []:
                cost = (option.get("shippingCost") or {}).get("value")
                if cost is not None:
                    shipping = _money(cost)
                    break
                if option.get("shippingCostType") == "FREE":
                    shipping = 0.0
                    break
            availability = "in_stock"  # search returns active listings only
            for est in item.get("estimatedAvailabilities") or []:
                if est.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
                    availability = "out_of_stock"
            offers.append(
                Offer(
                    retailer="eBay",
                    seller=seller_info.get("username"),
                    price=price,
                    currency=price_info.get("currency") or "USD",
                    shipping=shipping,
                    availability=availability,
                    condition=_condition_class(item.get("condition")),
                    url=item["itemWebUrl"],
                    source_api=self.api_label,
                    title=item.get("title") or "",
                    upc=str(item["gtin"]) if item.get("gtin") else None,
                    mpn=None,
                    brand=None,
                    queried_by_upc=bool(identity.upc),
                )
            )
        return offers, dropped


# ---------------------------------------------------------------------------
# Tier B — v1 extractor over direct-store pages


class DirectStoreSource:
    """Tier B: the v1 check_price extractor aimed at a caller-supplied URL."""

    name = "direct"
    api_label = "direct_page_extractor"

    def __init__(self, extractor=None) -> None:  # noqa: ANN001
        self._extractor = extractor or check_price

    def configured(self) -> bool:
        return True  # no key needed; fires only when a URL is in play

    def _offer_from_page(self, url: str, info, identity_source: bool) -> Offer:  # noqa: ANN001
        host = urllib.parse.urlsplit(url).hostname or ""
        label = host.removeprefix("www.").split(".")[0] or host
        availability_raw = (info.availability or "").lower()
        availability = (
            "in_stock"
            if "instock" in availability_raw.replace("_", "")
            else "out_of_stock"
            if "outofstock" in availability_raw.replace("_", "")
            else "unknown"
        )
        return Offer(
            retailer=label.capitalize(),
            seller=host,
            price=round(float(info.price), 2),
            currency=info.currency,
            shipping=None,  # page extractors do not prove shipping
            availability=availability,
            condition="new",
            url=url,
            source_api=self.api_label,
            title=info.title or "",
            identity_source=identity_source,
        )

    def fetch_offer(self, url: str) -> Offer | None:
        """Best-effort single-page offer; None when the page yields no price."""
        try:
            info = self._extractor(url)
        except PriceError:
            return None
        return self._offer_from_page(url, info, identity_source=False)

    def resolve_identity(self, url: str) -> tuple[Identity, Offer]:
        """Read the identity off the page itself (spec section 1, URL input)."""
        from .bestprice import _extract_brand, _extract_mpn

        try:
            info = self._extractor(url)
        except PriceError as exc:
            raise ValueError(f"could not read product identity from {url}: {exc}") from exc
        identity = Identity(
            kind="url",
            brand=_extract_brand(info.title or ""),
            mpn=_extract_mpn(info.title or ""),
            title=info.title,
            source_url=url,
        )
        return identity, self._offer_from_page(url, info, identity_source=True)

    def search(self, identity: Identity, condition: str, country: str):
        # Tier B has no search surface: it answers the URL that defined the
        # identity (handled by the orchestrator), never a scraped search page.
        return [], 0


def default_sources() -> list:
    """The v2.0 source set: Tier A APIs (key-gated) + Tier B extractor."""
    return [BestBuySource(), WalmartSource(), EbaySource(), DirectStoreSource()]


__all__ = [
    "BestBuySource",
    "DirectStoreSource",
    "EbaySource",
    "SourceError",
    "WalmartSource",
    "default_sources",
    "fetch_json",
    "walmart_signature",
]
