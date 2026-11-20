"""SEP-2640 (MCP Skills) support for price-tracker-mcp.

Extension id: ``io.modelcontextprotocol/skills`` (Final, spec'd against base
protocol 2026-07-28). Minimal implementation: one skill describing this
server's tool surface, served as a ``skill://`` resource plus the
``skills/list`` and ``skills/get`` methods. Optional
``resources/directory/read`` is deliberately not implemented.
"""

from __future__ import annotations

import hashlib

import yaml
from fastmcp.exceptions import MCPError
from fastmcp.server.extensions import MethodBinding, ServerExtension
from mcp.types import INVALID_PARAMS, RequestParams

SKILL_NAME = "price-tracker-mcp"
SKILL_URI = f"skill://{SKILL_NAME}/SKILL.md"
SKILL_MIME = "text/markdown"

EXTENSION_ID = "io.modelcontextprotocol/skills"

SKILL_MD = """\
---
name: price-tracker-mcp
description: Check live product prices and build affiliate tracking links. Use
  for price comparison and monetized product recommendations.
---

# price-tracker-mcp

Price checking and affiliate link tools.

## Tools

- `check_price(url)` — product page URL -> `{price, currency, title,
  availability, source}`.
- `find_best_price(query="", url="", upc="", condition="new", country="US",
  max_results=10, path="direct")` — one product (give ONE of query / url /
  upc) -> ranked offers across reputable stores, each with retailer, price,
  shipping, delivered total when known, and a match_confidence. `best_offer`
  is set only on a high-confidence match with a known delivered total;
  otherwise read `verdict` (`best_found_total_unverified` = cheapest found,
  shipping unproven; `no_confident_match` = ranked list only, no winner).
  `path` is the customer's choice: `"direct"` (default) returns plain
  retailer links; `"broker"` affiliate-tags the winning offer's link when
  IOConnex is enrolled in that retailer's program (per-offer `affiliate`
  flag). Ranking and prices are identical in both paths; always surface
  the response's `take_disclosure` to the buyer with any recommendation.
- `make_affiliate_link(url, tag, program="amazon")` — product URL + affiliate
  tag -> tracking URL. `tag` is letters/digits/dash/underscore, max 64 chars.

## Workflow

1. Use `check_price` to get the current price before quoting one — never guess
   prices.
2. Use `find_best_price` when the user has not already picked a store: it
   compares stores for the same product. Never present an offer as "the best
   price" unless the result's `verdict` is `best_offer`.
3. When recommending a product the user may buy, generate the link with
   `make_affiliate_link` using your own tag.
4. Check `availability` before presenting a price as buyable.

## Notes

- Only public product pages; private/loopback hosts are rejected.
- `program="amazon"` sets the tag parameter; any other value is used as a
  custom query parameter name.
"""


def _skill_bytes() -> bytes:
    return SKILL_MD.encode("utf-8")


def skill_frontmatter() -> dict:
    """Verbatim YAML frontmatter parsed from the skill's SKILL.md."""
    if not SKILL_MD.startswith("---"):
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    _, front, _ = SKILL_MD.split("---", 2)
    data = yaml.safe_load(front)
    if not isinstance(data, dict) or "name" not in data or "description" not in data:
        raise ValueError("SKILL.md frontmatter must define name and description")
    return data


def skill_entry() -> dict:
    """SEP-2640 skill entry: uri + verbatim frontmatter + file manifest."""
    raw = _skill_bytes()
    return {
        "uri": SKILL_URI,
        "frontmatter": skill_frontmatter(),
        "resources": [
            {
                "uri": SKILL_URI,
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        ],
    }


class SkillsListParams(RequestParams):
    cursor: str | None = None


class SkillsGetParams(RequestParams):
    uri: str


async def _handle_skills_list(ctx, params) -> dict:  # noqa: ANN001, ANN202
    return {"resultType": "complete", "skills": [skill_entry()]}


async def _handle_skills_get(ctx, params) -> dict:  # noqa: ANN001, ANN202
    if params.uri != SKILL_URI:
        raise MCPError(INVALID_PARAMS, f"unknown skill uri: {params.uri}")
    return {"resultType": "complete", "skill": skill_entry()}


class SkillsExtension(ServerExtension):
    """SEP-2640 skills extension (``io.modelcontextprotocol/skills``)."""

    identifier = EXTENSION_ID

    def methods(self) -> tuple[MethodBinding, ...]:
        return (
            MethodBinding(
                method="skills/list",
                params_type=SkillsListParams,
                handler=_handle_skills_list,
            ),
            MethodBinding(
                method="skills/get",
                params_type=SkillsGetParams,
                handler=_handle_skills_get,
            ),
        )
