"""End-to-end MCP protocol tests via FastMCP's in-memory client."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from price_tracker import price as price_mod
from price_tracker.mcp_server import mcp


def _run(coro):
    return asyncio.run(coro)


_OG_HTML = """<html><head>
<title>Widget Pro</title>
<meta property="og:price:amount" content="79.99" />
<meta property="og:price:currency" content="USD" />
</head><body></body></html>"""


@pytest.fixture()
def stubbed_backends(monkeypatch):
    monkeypatch.setattr(price_mod, "fetch_html", lambda url, timeout=20: _OG_HTML)


def test_both_tools_registered():
    async def go():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return {t.name for t in tools}

    assert _run(go()) == {"check_price", "make_affiliate_link"}


def test_tool_descriptions_present():
    async def go():
        async with Client(mcp) as client:
            return await client.list_tools()

    for tool in _run(go()):
        assert tool.description, f"tool {tool.name} has no description"


def test_mcp_check_price_roundtrip(stubbed_backends):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("check_price", {"url": "https://example.com/p"})

    result = _run(go())
    text = "".join(c.text for c in result.content if hasattr(c, "text"))
    assert "79.99" in text
    assert "USD" in text


def test_mcp_make_affiliate_link_roundtrip():
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(
                "make_affiliate_link",
                {"url": "https://www.amazon.com/dp/B0X", "tag": "mytag-20"},
            )

    result = _run(go())
    text = "".join(c.text for c in result.content if hasattr(c, "text"))
    assert "tag=mytag-20" in text
