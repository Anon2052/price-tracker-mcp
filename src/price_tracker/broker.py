"""Broker layer — products lane (broker-layer-spec.md sections 1-3, 5).

``find_best_price`` offers two paths and the customer picks one. Direct
(the default) returns plain retailer links; IOConnex earns only the lookup
fee. Broker returns the same ranked list — ranking is computed before this
module ever runs, and this module never reorders, adds, or drops offers —
with the winning offer's link affiliate-tagged when (and only when) we are
enrolled in that retailer's affiliate program.

Enrollment is program state in env config, deliberately separate from the
Tier A API keys in ``sources.py``: an API key is not an affiliate
enrollment. Best Buy Products API access, the eBay Browse API, and the
Walmart Affiliate API make a source *live*; earning requires the
retailer's affiliate program approved separately. A retailer whose tag env
var is unset is ``affiliate: false`` even if its API is answering.

Per program, env config (values are secrets/program IDs — names only in
the repo; see render.yaml ``sync: false`` entries):

- ``<P>_AFFILIATE_TAG``: our affiliate tag/ID for the program. Its
  presence IS the enrollment signal.
- ``<P>_AFFILIATE_PARAM`` (optional): query parameter name the program
  expects the tag in; a house default per program is used otherwise.
- ``<P>_AFFILIATE_RATE`` (optional): the commission rate the program
  publishes for us, disclosed verbatim as ``take_rate_if_known``. Never
  estimated — unset means ``null``.

Amazon is out of scope for this layer (not a v2 source; its
``make_affiliate_link`` support is unchanged).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from .affiliate import AffiliateError, make_affiliate_link

logger = logging.getLogger("price_tracker.broker")

PATHS = ("direct", "broker")
PATHS_OFFERED = ["direct", "broker"]

DIRECT_DISCLOSURE = (
    "Direct path: these links are not affiliate links. "
    "IOConnex earns only the lookup fee for this call."
)
NO_AFFILIATE_DISCLOSURE = (
    "No affiliate arrangement applies to these offers; "
    "IOConnex earns only the lookup fee for this call."
)
WINNER_NOT_ENROLLED_DISCLOSURE = (
    "No affiliate arrangement applies to the recommended offer; "
    "IOConnex earns only the lookup fee for this call."
)
BROKER_DISCLOSURE_TEMPLATE = (
    "Broker path: IOConnex may earn a commission from {retailer} if you "
    "buy through this link. The commission is paid by the retailer — it "
    "is not added to your price. Offers are ranked by your lowest "
    "delivered total, not by commission."
)


@dataclass(frozen=True)
class AffiliateProgram:
    """One retailer's affiliate program, as env-driven enrollment state."""

    key: str  # matches the source name / source_api prefix in sources.py
    label: str  # retailer display name used in the disclosure sentence
    retailer_names: frozenset[str]  # normalized `retailer` values on offers
    tag_env: str
    param_env: str
    default_param: str
    rate_env: str


PROGRAMS: tuple[AffiliateProgram, ...] = (
    AffiliateProgram(
        key="bestbuy",
        label="Best Buy",
        retailer_names=frozenset({"bestbuy", "best buy"}),
        tag_env="BESTBUY_AFFILIATE_TAG",
        param_env="BESTBUY_AFFILIATE_PARAM",
        default_param="ref",
        rate_env="BESTBUY_AFFILIATE_RATE",
    ),
    AffiliateProgram(
        key="walmart",
        label="Walmart",
        retailer_names=frozenset({"walmart"}),
        tag_env="WALMART_AFFILIATE_TAG",
        param_env="WALMART_AFFILIATE_PARAM",
        default_param="affp1",
        rate_env="WALMART_AFFILIATE_RATE",
    ),
    AffiliateProgram(
        key="ebay",
        label="eBay",
        retailer_names=frozenset({"ebay"}),
        tag_env="EBAY_AFFILIATE_TAG",
        param_env="EBAY_AFFILIATE_PARAM",
        default_param="mkcid",
        rate_env="EBAY_AFFILIATE_RATE",
    ),
)


def _norm_retailer(name: str | None) -> str:
    return " ".join((name or "").strip().lower().split())


def program_for_offer(offer: dict) -> AffiliateProgram | None:
    """The affiliate program an offer belongs to, enrolled or not."""
    retailer = _norm_retailer(offer.get("retailer"))
    source_api = (offer.get("source_api") or "").lower()
    for program in PROGRAMS:
        if retailer in program.retailer_names or source_api.startswith(program.key):
            return program
    return None


def enrolled_tag(
    program: AffiliateProgram, environ: Mapping[str, str] | None = None
) -> tuple[str, str, str | None] | None:
    """(tag, query_param, published_rate) if enrolled in `program`.

    Enrollment = the program's tag env var is set to a non-empty value.
    The published rate passes through verbatim (stripped) or is None —
    this layer never estimates a commission rate.
    """
    env = os.environ if environ is None else environ
    tag = (env.get(program.tag_env) or "").strip()
    if not tag:
        return None
    param = (env.get(program.param_env) or "").strip() or program.default_param
    rate = (env.get(program.rate_env) or "").strip() or None
    return tag, param, rate


def _winner(result: dict) -> dict | None:
    """The offer the verdict chose: best_offer, else best_found, else None."""
    return result.get("best_offer") or result.get("best_found")


def _same_offer(a: dict, b: dict) -> bool:
    """Same listed offer, ignoring the URL (the winner's gets retagged)."""
    keys = ("retailer", "price", "source_api", "title")
    return all(a.get(k) == b.get(k) for k in keys)


def _offer_identity_hash(offer: dict) -> str:
    raw = "|".join(str(offer.get(k) or "") for k in ("retailer", "source_api", "url", "title"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_path(result: dict, path: str, environ: Mapping[str, str] | None = None) -> dict:
    """Return a copy of a find_best_price result dressed for `path`.

    Adds ``paths_offered``, ``path_chosen``, per-offer ``affiliate`` flags,
    ``take_disclosure``, and ``take_rate_if_known``. On the broker path the
    winning offer's link (and its twin entry in the ranked list) is tagged
    via ``make_affiliate_link`` when its retailer is enrolled. The input
    is never mutated — the orchestrator caches the undressed result, so
    direct and broker calls share one fan-out.
    """
    out = copy.deepcopy(result)
    offers = out.get("offers") or []
    winner = _winner(out)
    out["paths_offered"] = list(PATHS_OFFERED)
    out["path_chosen"] = path
    out["take_rate_if_known"] = None

    if path == "direct":
        for offer in offers:
            offer["affiliate"] = False
        if winner is not None:
            winner["affiliate"] = False
        out["take_disclosure"] = DIRECT_DISCLOSURE
        return out

    # Broker path.
    winner_enrolled = False
    if winner is not None:
        program = program_for_offer(winner)
        enrollment = enrolled_tag(program, environ) if program else None
        if enrollment is not None:
            tag, param, rate = enrollment
            plain_url = winner["url"]
            try:
                tagged_url = make_affiliate_link(plain_url, tag, program=param)
            except AffiliateError as exc:
                # A malformed operator-supplied tag must not sink the
                # lookup; fall back to the plain link, disclosed as such.
                logger.warning("broker tagging failed for %s: %s", program.key, exc)
            else:
                winner_enrolled = True
                identity_hash = _offer_identity_hash(winner)  # pre-tag identity
                winner["url"] = tagged_url
                winner["affiliate"] = True
                for offer in offers:
                    if offer["url"] == plain_url and _same_offer(offer, winner):
                        offer["url"] = tagged_url
                        offer["affiliate"] = True
                out["take_disclosure"] = BROKER_DISCLOSURE_TEMPLATE.format(retailer=program.label)
                out["take_rate_if_known"] = rate
                logger.info(
                    "broker_attribution %s",
                    json.dumps(
                        {
                            "retailer": program.label,
                            "offer_identity_hash": identity_hash,
                            "verdict": out.get("verdict"),
                            "take_rate_if_known": rate,
                        },
                        sort_keys=True,
                    ),
                )
    if not winner_enrolled:
        if winner is None:
            # Nothing was chosen, so nothing can earn: the plain
            # no-arrangement wording is the truthful one even when an
            # enrolled retailer's offers sit untagged in the list.
            out["take_disclosure"] = NO_AFFILIATE_DISCLOSURE
        else:
            any_enrolled_offer = any(
                (p := program_for_offer(o)) is not None and enrolled_tag(p, environ) is not None
                for o in offers
            )
            out["take_disclosure"] = (
                WINNER_NOT_ENROLLED_DISCLOSURE if any_enrolled_offer else NO_AFFILIATE_DISCLOSURE
            )
    for offer in offers:
        offer.setdefault("affiliate", False)
    if winner is not None:
        winner.setdefault("affiliate", winner_enrolled)
    return out
