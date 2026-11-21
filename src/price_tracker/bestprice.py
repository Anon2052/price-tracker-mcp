"""find_best_price: multi-source best-price comparison (Price Tracker v2).

Fans out across reputable sources (Tier A official retailer APIs + the
Tier B v1 page extractor), normalizes every offer to one product identity,
and returns a ranked, evidence-linked offer list. The verdict discipline is
the product: ``best_offer`` is named ONLY when the top offer is a
high-confidence product match AND its delivered total is known AND it is in
stock. Otherwise the result is labeled ``best_found_total_unverified``
(shipping unknown) or the tool abstains (``no_confident_match``) and returns
the ranked list without a winner.

Tier C (licensed aggregator) is OFF in v2.0 by spec: no aggregator code
exists in this build. Amazon direct is OUT (PA-API retired; Creators API
gated) — Amazon offers can only ever arrive via a future Tier C.

Matching (spec section 3): UPC/GTIN exact match is the gold key, then
brand + manufacturer part number, then title similarity (capped below the
high-confidence bar, so a title-only match can never be named best_offer).
Variants are different products: storage size, pack count, and model year
extracted from titles must agree or the offer is demoted and labeled
``variant_mismatch``.
"""

from __future__ import annotations

import copy
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import broker as _broker

HIGH_CONFIDENCE = 0.85  # minimum match_confidence for a best_offer verdict
MIN_RANK_CONFIDENCE = 0.5  # minimum confidence to enter the winner pool
CACHE_TTL_SECONDS = 6 * 3600  # spec section 6: per-identity cache, 6h max

CONDITIONS = ("new", "used", "refurbished", "any")
# Which normalized offer conditions each requested condition admits. Used and
# refurbished stay in their own buckets unless the caller explicitly asks;
# they are never silently mixed into the new-price ranking (spec section 2).
CONDITION_BUCKETS: dict[str, frozenset[str]] = {
    "new": frozenset({"new"}),
    "used": frozenset({"used", "open_box"}),
    "refurbished": frozenset({"refurbished"}),
    "any": frozenset({"new", "used", "refurbished", "open_box"}),
}


class BestPriceError(Exception):
    """Raised when a find_best_price call cannot be answered at all."""


# ---------------------------------------------------------------------------
# Identity + offers


@dataclass
class Identity:
    """The one product every offer is normalized against."""

    kind: str  # "upc" | "url" | "query"
    upc: str | None = None
    brand: str | None = None
    mpn: str | None = None
    title: str | None = None
    source_url: str | None = None

    def cache_key(self) -> str:
        if self.upc:
            return f"upc:{self.upc}"
        if self.source_url:
            return f"url:{self.source_url}"
        return f"title:{_norm_text(self.title or '')}"


@dataclass
class Offer:
    """One retailer's offer, normalized across sources."""

    retailer: str
    seller: str | None
    price: float
    currency: str
    shipping: float | None  # None = unknown (NOT zero)
    availability: str  # "in_stock" | "out_of_stock" | "unknown"
    condition: str  # "new" | "used" | "refurbished" | "open_box"
    url: str
    source_api: str  # e.g. "bestbuy_products_api", "ebay_browse_api"
    title: str
    upc: str | None = None
    mpn: str | None = None
    brand: str | None = None
    queried_by_upc: bool = False  # source found it via a UPC-targeted query
    identity_source: bool = False  # Tier B page that defined the identity
    # Assigned by the matcher:
    match_confidence: float = 0.0
    variant_mismatch: bool = False

    @property
    def total(self) -> float | None:
        """Delivered total, only when shipping is actually known."""
        if self.shipping is None:
            return None
        return round(self.price + self.shipping, 2)

    def to_dict(self, fetched_at: str) -> dict:
        return {
            "retailer": self.retailer,
            "seller": self.seller,
            "price": self.price,
            "currency": self.currency,
            "shipping": self.shipping,
            "total_if_known": self.total,
            "availability": self.availability,
            "condition": self.condition,
            "url": self.url,
            "source_api": self.source_api,
            "fetched_at": fetched_at,
            "match_confidence": self.match_confidence,
            "title": self.title,
            "variant_mismatch": self.variant_mismatch,
        }


# ---------------------------------------------------------------------------
# Identifier + title parsing


def normalize_upc(raw: str | None) -> str | None:
    """Normalize a UPC/GTIN/EAN string to bare digits, or None if invalid."""
    if not raw:
        return None
    digits = re.sub(r"[\s\-]", "", raw.strip())
    if digits.isdigit() and len(digits) in (8, 12, 13, 14):
        return digits
    return None


def upc_variants(upc: str) -> set[str]:
    """Equivalent encodings of one GTIN (UPC-A 12 <-> EAN-13 with lead 0)."""
    out = {upc}
    if len(upc) == 12:
        out.add("0" + upc)
    if len(upc) == 13 and upc.startswith("0"):
        out.add(upc[1:])
    return out


def _norm_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _norm_code(code: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (code or "").lower())


# A frozen, deliberately small brand lexicon for query parsing. Brand is a
# confidence signal, never a gate; unknown brands simply score title-only.
KNOWN_BRANDS = (
    "samsung",
    "apple",
    "lg",
    "sony",
    "bosch",
    "garmin",
    "lenovo",
    "dell",
    "hp",
    "bose",
    "dyson",
    "kitchenaid",
    "whirlpool",
    "nintendo",
    "microsoft",
    "canon",
    "nikon",
    "dewalt",
    "milwaukee",
    "shark",
    "ninja",
    "keurig",
    "logitech",
    "anker",
    "jbl",
    "philips",
    "panasonic",
    "tcl",
    "hisense",
    "vizio",
    "asus",
    "acer",
    "razer",
    "seagate",
    "sandisk",
    "crucial",
    "kingston",
    "fitbit",
    "google",
    "makita",
    "ryobi",
    "honeywell",
    "irobot",
    "instant",
    "cuisinart",
    "frigidaire",
    "maytag",
    "casio",
    "fossil",
)


def _extract_brand(text: str) -> str | None:
    tokens = _norm_text(text).split()
    for token in tokens:
        if token in KNOWN_BRANDS:
            return token
    return None


_MPN_RE = re.compile(
    r"\b(?=[A-Za-z0-9\-/]*\d)(?=[A-Za-z0-9\-/]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9\-/]{3,19}\b"
)


def _extract_mpn(text: str) -> str | None:
    """Best-effort manufacturer part number: a mixed letter+digit token."""
    for match in _MPN_RE.finditer(text or ""):
        token = match.group(0)
        if _extract_variant_attrs(token):  # storage/year tokens are not MPNs
            continue
        if token.lower() in KNOWN_BRANDS:
            continue
        return token
    return None


_STORAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(tb|gb|mb)\b", re.IGNORECASE)
_PACK_RES = (
    re.compile(r"pack\s*of\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*[- ]\s*pack\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*(?:count|ct)\b", re.IGNORECASE),
)
_YEAR_RE = re.compile(r"\b(20[1-3]\d)\b")


def _extract_variant_attrs(text: str) -> dict[str, str]:
    """Variant-defining attributes: storage size, pack count, model year.

    Variants are different products (spec section 3): when both the identity
    and an offer state one of these, they must agree.
    """
    attrs: dict[str, str] = {}
    m = _STORAGE_RE.search(text or "")
    if m:
        attrs["storage"] = f"{m.group(1)}{m.group(2).upper()}"
    for rx in _PACK_RES:
        m = rx.search(text or "")
        if m:
            attrs["pack"] = m.group(1)
            break
    m = _YEAR_RE.search(text or "")
    if m:
        attrs["year"] = m.group(1)
    return attrs


_STOPWORDS = frozenset({"the", "a", "an", "with", "for", "and", "of", "new", "by", "in", "on"})


def _title_tokens(text: str) -> set[str]:
    return {t for t in _norm_text(text).split() if t not in _STOPWORDS and len(t) > 1}


def _title_similarity(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def identity_from_query(query: str) -> Identity:
    """Resolve a free-text product query into an Identity."""
    upc = None
    for token in re.findall(r"[\d][\d\s\-]{6,}\d", query):
        upc = normalize_upc(token)
        if upc:
            break
    return Identity(
        kind="upc" if upc else "query",
        upc=upc,
        brand=_extract_brand(query),
        mpn=_extract_mpn(query),
        title=query.strip(),
    )


def identity_from_upc(upc: str) -> Identity:
    normalized = normalize_upc(upc)
    if normalized is None:
        raise BestPriceError(f"invalid UPC/GTIN/EAN: {upc!r}")
    return Identity(kind="upc", upc=normalized)


# ---------------------------------------------------------------------------
# Matching + verdict


def score_match(identity: Identity, offer: Offer) -> tuple[float, bool]:
    """Score one offer against the identity. Returns (confidence, variant_mismatch).

    Confidence ladder (spec section 3): UPC exact = 1.0; the source's own UPC
    index (payload did not echo a UPC) = 0.9; brand+MPN = 0.9; title-only is
    capped at 0.84 so it can never clear the high-confidence bar alone.
    """
    identity_attrs = _extract_variant_attrs(identity.title or "")
    offer_attrs = _extract_variant_attrs(offer.title or "")
    mismatch = any(
        key in offer_attrs and identity_attrs[key] != offer_attrs[key] for key in identity_attrs
    )
    if offer.identity_source:
        # The Tier B page that defined the identity matches it by construction.
        return 0.95, False
    if identity.upc and offer.upc:
        same = bool(upc_variants(identity.upc) & upc_variants(offer.upc))
        return (1.0 if same else 0.0), mismatch
    if identity.mpn and offer.mpn and _norm_code(identity.mpn) == _norm_code(offer.mpn):
        if identity.brand and offer.brand and _norm_text(identity.brand) != _norm_text(offer.brand):
            return 0.45, mismatch
        return 0.9, mismatch
    if offer.queried_by_upc and identity.upc:
        return 0.9, mismatch
    # Title path.
    if not identity.title or not offer.title:
        return 0.3, mismatch
    sim = _title_similarity(identity.title, offer.title)
    confidence = round(0.4 + 0.45 * sim, 2)
    if identity.upc:
        confidence = min(confidence, 0.8)  # gold key unverified for this offer
    if identity.brand and offer.brand and _norm_text(identity.brand) != _norm_text(offer.brand):
        confidence = min(confidence, 0.3)
    return min(confidence, 0.84), mismatch


def rank_and_verdict(
    identity: Identity,
    offers: list[Offer],
    condition: str,
) -> tuple[list[Offer], str, Offer | None, Offer | None]:
    """Filter by condition, score, rank, and decide the verdict.

    Returns (ranked_offers, verdict, best_offer, best_found). The winner pool
    is in-stock, variant-clean, condition-matching offers with at least
    MIN_RANK_CONFIDENCE; known delivered totals rank ahead of unknown ones
    inside the pool, so an unverified total can never beat a verified one.
    """
    allowed = CONDITION_BUCKETS[condition]
    kept = [o for o in offers if o.condition in allowed]
    for offer in kept:
        offer.match_confidence, offer.variant_mismatch = score_match(identity, offer)
    pool = [
        o
        for o in kept
        if o.availability == "in_stock"
        and not o.variant_mismatch
        and o.match_confidence >= MIN_RANK_CONFIDENCE
    ]
    pool.sort(key=lambda o: (o.total is None, o.total if o.total is not None else o.price, o.price))
    rest = [o for o in kept if o not in pool]
    rest.sort(key=lambda o: (-o.match_confidence, o.price))
    ranked = pool + rest
    # A winner must be high-confidence AND carry a known delivered total;
    # pool order already puts the cheapest verified totals first.
    winners = [o for o in pool if o.match_confidence >= HIGH_CONFIDENCE and o.total is not None]
    if winners:
        return ranked, "best_offer", winners[0], None
    unverified = [o for o in pool if o.match_confidence >= HIGH_CONFIDENCE]
    if unverified:
        cheapest = min(unverified, key=lambda o: o.price)
        return ranked, "best_found_total_unverified", None, cheapest
    return ranked, "no_confident_match", None, None


# ---------------------------------------------------------------------------
# Result cache (spec section 6: per-identity, 6h max, in-process)


class ResultCache:
    """Thread-safe in-process TTL cache. Resets when the service restarts."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS, clock=time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, payload = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[key]
                return None
            return payload

    def put(self, key: str, payload: dict) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), payload)
            if len(self._entries) > 5000:  # memory hygiene
                self._entries.clear()


_DEFAULT_CACHE = ResultCache()


# ---------------------------------------------------------------------------
# Orchestration


@dataclass
class SourceOutcome:
    name: str
    status: str  # "ok" | "not_configured" | "error" | "skipped"
    offers: list[Offer] = field(default_factory=list)
    detail: str | None = None
    filtered_out: int = 0


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_best_price(
    query: str | None = None,
    url: str | None = None,
    upc: str | None = None,
    condition: str = "new",
    country: str = "US",
    max_results: int = 10,
    path: str = "direct",
    sources: list | None = None,
    cache: ResultCache | None = None,
) -> dict:
    """Compare prices for one product across all configured sources.

    Exactly one primary input is expected (upc, url, or query); when several
    are given, identity resolves in that precedence order and the rest act as
    supporting evidence. Raises BestPriceError when no input is usable or a
    URL-only identity cannot be read.

    ``path`` is the broker-layer customer choice (broker-layer-spec.md
    section 1): "direct" (default) returns plain retailer links; "broker"
    affiliate-tags the winning offer's link when IOConnex is enrolled in
    that retailer's program. Ranking, verdict, and per-offer prices are
    computed once, before the path is applied, and are identical in both
    paths; the lookup costs the same $0.03 in both.
    """
    from .sources import DirectStoreSource, default_sources

    condition = (condition or "new").strip().lower()
    if condition not in CONDITION_BUCKETS:
        raise BestPriceError(
            f"condition must be one of {sorted(CONDITION_BUCKETS)}, got {condition!r}"
        )
    path = (path or "direct").strip().lower()
    if path not in _broker.PATHS:
        raise BestPriceError(f"path must be one of {list(_broker.PATHS)}, got {path!r}")
    country = (country or "US").strip().upper() or "US"
    max_results = max(1, min(int(max_results or 10), 50))

    if sources is None:
        sources = default_sources()
    cache = cache if cache is not None else _DEFAULT_CACHE

    # --- Resolve identity -------------------------------------------------
    identity: Identity | None = None
    tier_b_offer: Offer | None = None
    direct = next((s for s in sources if isinstance(s, DirectStoreSource)), None)
    if upc and normalize_upc(upc):
        identity = identity_from_upc(upc)
        if url and direct is not None:
            tier_b_offer = direct.fetch_offer(url)  # scored against the UPC
    elif url:
        if direct is None:
            raise BestPriceError("URL input requires the Tier B direct-store source")
        identity, tier_b_offer = direct.resolve_identity(url)
        if query:
            identity.brand = identity.brand or _extract_brand(query)
            identity.mpn = identity.mpn or _extract_mpn(query)
    elif query:
        identity = identity_from_query(query)
    if identity is None:
        raise BestPriceError("provide one of: query, url, or upc")

    cache_key = "|".join([identity.cache_key(), condition, country, str(max_results)])
    cached = cache.get(cache_key)
    if cached is not None:
        # The cache holds the path-neutral result; the customer's path is
        # applied to a copy, so direct and broker calls share one fan-out.
        result = _broker.apply_path(cached, path)
        result["cache"] = {"hit": True}
        return result

    fetched_at = _now_iso()

    # --- Fan out in parallel ----------------------------------------------
    def run_source(source) -> SourceOutcome:  # noqa: ANN001, ANN202
        name = source.name
        if not source.configured():
            return SourceOutcome(name, "not_configured", detail="API key not set")
        try:
            found, filtered = source.search(identity, condition, country)
            return SourceOutcome(name, "ok", offers=found, filtered_out=filtered)
        except Exception as exc:  # one dead source must not sink the call
            return SourceOutcome(name, "error", detail=str(exc)[:200])

    api_sources = [s for s in sources if not isinstance(s, DirectStoreSource)]
    outcomes: dict[str, SourceOutcome] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(api_sources))) as pool:
        for outcome in pool.map(run_source, api_sources):
            outcomes[outcome.name] = outcome
    if tier_b_offer is not None:
        outcomes[direct.name] = SourceOutcome(direct.name, "ok", offers=[tier_b_offer])
    elif direct is not None:
        outcomes[direct.name] = SourceOutcome(
            direct.name,
            "skipped",
            detail="Tier B answers URL inputs and known direct-store pages",
        )

    all_offers = [o for outcome in outcomes.values() for o in outcome.offers]
    ranked, verdict, best, best_found = rank_and_verdict(identity, all_offers, condition)
    ranked = ranked[:max_results]

    notes: list[str] = []
    if country != "US":
        notes.append("Tier A sources are US-market APIs; prices are USD offers.")
    if verdict == "no_confident_match" and ranked:
        notes.append("No offer cleared the bar for a named best offer; ranked list only.")
    unconfigured = [n for n, o in outcomes.items() if o.status == "not_configured"]
    if unconfigured:
        notes.append("Sources not yet live (no API key configured): " + ", ".join(unconfigured))

    result = {
        "identity": {
            "kind": identity.kind,
            "upc": identity.upc,
            "brand": identity.brand,
            "mpn": identity.mpn,
            "title": identity.title,
            "source_url": identity.source_url,
        },
        "condition": condition,
        "country": country,
        "verdict": verdict,
        "best_offer": best.to_dict(fetched_at) if best else None,
        "best_found": best_found.to_dict(fetched_at) if best_found else None,
        "offers": [o.to_dict(fetched_at) for o in ranked],
        "source_status": {
            name: {
                "status": o.status,
                "offers": len(o.offers),
                "filtered_out": o.filtered_out,
                **({"detail": o.detail} if o.detail else {}),
            }
            for name, o in sorted(outcomes.items())
        },
        "fetched_at": fetched_at,
        "cache": {"hit": False},
        "notes": notes,
    }
    # Cache only results at least one source actually answered; a total
    # outage should be retried on the next call, not memoized for 6h. The
    # cached copy stays path-neutral — the broker layer is applied per
    # call, on the way out.
    if any(o.status == "ok" for o in outcomes.values()):
        cache.put(cache_key, copy.deepcopy(result))
    return _broker.apply_path(result, path)
