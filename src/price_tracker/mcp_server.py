"""FastMCP server: price checking + affiliate link tools over streamable HTTP."""

from __future__ import annotations

from dataclasses import asdict

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import affiliate as _affiliate
from . import bestprice as _bestprice
from . import price as _price
from .skills import (
    SKILL_MD,
    SKILL_MIME,
    SKILL_NAME,
    SKILL_URI,
    SkillsExtension,
    skill_frontmatter,
)

mcp = FastMCP(
    "price-tracker-mcp",
    instructions=(
        "Price tracking tools. "
        "check_price: product page URL -> {price, currency, title, availability, source}. "
        "find_best_price: product query, product URL, or UPC/GTIN -> ranked offers "
        "across reputable stores with per-offer evidence; a best_offer verdict is "
        "named only on a high-confidence match with a known delivered total. "
        "make_affiliate_link: product URL + tag -> URL with affiliate tracking applied. "
        "Private/loopback hosts are rejected. "
        f"Skill: {SKILL_URI} (usage instructions for these tools; "
        "SEP-2640: enumerate via skills/list, fetch one via skills/get)."
    ),
)


@mcp.tool(
    name="check_price",
    title="Check Product Price",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
def check_price(url: str) -> dict:
    """Check the current price of a product page.

    Args:
        url: The http(s) product page URL. Private/loopback hosts are rejected.
    """
    return asdict(_price.check_price(url))


@mcp.tool(
    name="find_best_price",
    title="Find Best Price Across Stores",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
)
def find_best_price(
    query: str = "",
    url: str = "",
    upc: str = "",
    condition: str = "new",
    country: str = "US",
    max_results: int = 10,
    path: str = "direct",
) -> dict:
    """Find the best price for one product across reputable stores.

    Provide ONE of: a product query ("Samsung Galaxy S25 Ultra 256GB"), a
    product page URL (identity is read from the page), or a UPC / GTIN / EAN.
    Returns a ranked offer list with evidence per offer. ``best_offer`` is
    set only when the winner is a high-confidence product match with a known
    delivered total; otherwise ``verdict`` explains the abstention
    (``best_found_total_unverified`` or ``no_confident_match``).

    Args:
        query: Free-text product description.
        url: Product page URL (http(s); private/loopback hosts rejected).
        upc: UPC / GTIN / EAN digits.
        condition: 'new' (default), 'used', 'refurbished', or 'any'. Used and
            refurbished offers never mix into the new-price ranking.
        country: Market to search (default 'US'; Tier A sources are US APIs).
        max_results: Maximum offers to return (default 10, max 50).
        path: Customer's choice (broker layer): 'direct' (default) returns
            plain retailer links and IOConnex earns only the lookup fee;
            'broker' affiliate-tags the winning offer's link when IOConnex
            is enrolled in that retailer's program, disclosed in
            ``take_disclosure``. Ranking and prices are identical either
            way, and the lookup costs the same in both paths.
    """
    try:
        return _bestprice.find_best_price(
            query=query or None,
            url=url or None,
            upc=upc or None,
            condition=condition,
            country=country,
            max_results=max_results,
            path=path,
        )
    except _bestprice.BestPriceError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool(
    name="make_affiliate_link",
    title="Build Affiliate Link",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
def make_affiliate_link(url: str, tag: str, program: str = "amazon") -> str:
    """Build an affiliate tracking link for a product URL.

    Args:
        url: The http(s) product page URL.
        tag: Your affiliate tag (letters/digits/dash/underscore, max 64 chars).
        program: 'amazon' (sets the tag parameter) or a custom query parameter name.
    """
    return _affiliate.make_affiliate_link(url, tag, program=program)


@mcp.resource(
    SKILL_URI,
    mime_type=SKILL_MIME,
    name=SKILL_NAME,
    description=skill_frontmatter()["description"],
)
def skill_readme() -> str:
    """SEP-2640 skill payload: usage instructions for this server's tools."""
    return SKILL_MD


# SEP-2640 skills extension: skills/list + skills/get (+ capability declaration).
mcp.add_extension(SkillsExtension())
