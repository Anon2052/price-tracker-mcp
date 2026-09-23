"""make_affiliate_link: append an affiliate tracking tag to a product URL.

Supported programs:
- amazon: sets the `tag` query parameter (your Associates tag, e.g. "mytag-20")
- generic: sets a custom query parameter of your choosing

Only http(s) URLs are accepted. Tags are restricted to letters, digits,
dashes, and underscores to keep the output URL safe.
"""

from __future__ import annotations

import re
import urllib.parse

_TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PARAM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_PROGRAM_PARAMS = {
    "amazon": "tag",
}


class AffiliateError(Exception):
    """Raised when an affiliate link cannot be built."""


def make_affiliate_link(url: str, tag: str, program: str = "amazon") -> str:
    """Return the URL with the affiliate tracking tag applied."""
    if not _TAG_RE.match(tag):
        raise AffiliateError("tag: 1-64 chars, letters/digits/dash/underscore only")
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise AffiliateError(f"URL: malformed ({exc})") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AffiliateError("URL: only http(s) URLs with a hostname are allowed")
    program = program.lower()
    if program in _PROGRAM_PARAMS:
        param = _PROGRAM_PARAMS[program]
    elif _PARAM_RE.match(program):
        param = program
    else:
        raise AffiliateError(
            f"program: unknown '{program}' (use 'amazon' or a custom parameter name)"
        )
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != param]
    query.append((param, tag))
    new_query = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(parts._replace(query=new_query))
