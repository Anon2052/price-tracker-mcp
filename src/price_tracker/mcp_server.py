"""FastMCP server: price checking + affiliate link tools over streamable HTTP."""

from __future__ import annotations

from dataclasses import asdict

from fastmcp import FastMCP

from . import affiliate as _affiliate
from . import price as _price

mcp = FastMCP(
    "price-tracker-mcp",
    instructions=(
        "Price tracking tools. "
        "check_price: product page URL -> {price, currency, title, availability, source}. "
        "make_affiliate_link: product URL + tag -> URL with affiliate tracking applied. "
        "Private/loopback hosts are rejected."
    ),
)


@mcp.tool(name="check_price")
def check_price(url: str) -> dict:
    """Check the current price of a product page.

    Args:
        url: The http(s) product page URL. Private/loopback hosts are rejected.
    """
    return asdict(_price.check_price(url))


@mcp.tool(name="make_affiliate_link")
def make_affiliate_link(url: str, tag: str, program: str = "amazon") -> str:
    """Build an affiliate tracking link for a product URL.

    Args:
        url: The http(s) product page URL.
        tag: Your affiliate tag (letters/digits/dash/underscore, max 64 chars).
        program: 'amazon' (sets the tag parameter) or a custom query parameter name.
    """
    return _affiliate.make_affiliate_link(url, tag, program=program)
