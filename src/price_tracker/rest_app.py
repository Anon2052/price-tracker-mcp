"""Combined ASGI app: REST API + FastMCP (streamable HTTP) + /health.

Run with:  uvicorn price_tracker.rest_app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import os
import threading
import time
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from . import __version__
from . import affiliate as _affiliate
from . import bestprice as _bestprice
from . import price as _price
from . import x402 as x402_mod
from .affiliate import AffiliateError
from .bestprice import BestPriceError
from .billing import router as billing_router
from .billing import stripe_effective_enabled
from .legal_docs import LANDING_PAGE, PRIVACY_POLICY, REFUND_POLICY, SUPPORT_PAGE, TERMS_OF_SERVICE
from .mcp_server import mcp
from .price import PriceError
from .x402 import (
    X402PaymentMiddleware,
    _peer_is_cloudflare,
    warn_if_payments_live,
    x402_effective_enabled,
)

log = logging.getLogger("price_tracker")

DEFAULT_RATE_LIMIT = os.environ.get("RATE_LIMIT", "60/minute")


# Cloudflare edge ranges + peer check are the single source of truth in x402
# (x402 must not import rest_app — the dependency runs the other way).


def _client_ip_from_scope(scope) -> str:  # noqa: ANN001, ANN202
    """Proxy-aware client IP for rate limiting (M-3).

    The X-Forwarded-For first entry is trusted ONLY when the direct TCP peer
    is a non-global address (i.e. we are behind Render's load balancer or
    another proxy). A client connecting directly always has a global peer
    address, so a spoofed XFF from a direct connection is ignored.

    When the direct TCP peer is a Cloudflare edge IP (the domain is proxied
    through Cloudflare), CF-Connecting-IP is trusted instead — spoof-proof,
    because the header is honored only for verifiably Cloudflare peers.
    """
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }
    client = scope.get("client")
    peer = client[0] if client else ""
    if _peer_is_cloudflare(peer):
        # Proxied through Cloudflare: CF-Connecting-IP carries the real
        # client IP. The header is honored ONLY when the TCP peer is
        # verifiably a Cloudflare edge IP, so spoofing from any other
        # peer is impossible.
        cf_ip = headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            try:
                ipaddress.ip_address(cf_ip)
                return cf_ip
            except ValueError:
                pass  # garbage header: fall through to existing logic
    try:
        behind_proxy = not ipaddress.ip_address(peer).is_global
    except ValueError:
        behind_proxy = False
    if behind_proxy:
        first = headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first:
            try:
                ipaddress.ip_address(first)
                return first
            except ValueError:
                pass  # garbage XFF value: fall through to the peer
    return peer or "unknown"


def _rate_limit_key(request: Request) -> str:
    """slowapi key_func: one bucket per real client IP (M-3)."""
    return _client_ip_from_scope(request.scope)


def _mcp_scope_key(scope) -> str:  # noqa: ANN001, ANN202
    """Key func for the MCP rate-limit wrapper (takes the raw ASGI scope)."""
    return _client_ip_from_scope(scope)


def _parse_per_minute(limit: str) -> int:
    """Parse a slowapi-style limit string ('60/minute') to requests/minute."""
    try:
        count_raw, _, per = limit.strip().partition("/")
        count = int(count_raw)
        factor = {"second": 60, "minute": 1, "hour": 1 / 60, "day": 1 / 1440}.get(
            (per or "minute").strip().lower()
        )
        if factor is None or count <= 0:
            raise ValueError
        return max(1, int(count * factor))
    except (ValueError, AttributeError):
        log.warning("Unparseable RATE_LIMIT=%r; MCP limit defaulting to 60/minute", limit)
        return 60


class _McpRateLimitMiddleware:
    """Per-IP sliding-window rate limiter for the mounted MCP sub-app (H-5).

    SlowAPI's default_limits cannot see through a Starlette Mount (a Mount
    has no ``endpoint`` attribute, so SlowAPI's middleware exempts it),
    which left /mcp/* completely unthrottled. This pure-ASGI wrapper applies
    the same per-minute budget around the MCP app, covering both the /mcp/*
    mount and the bare-/mcp route.
    """

    def __init__(self, app, per_minute: int, key_func) -> None:  # noqa: ANN001, ANN204
        self._app = app
        self._per_minute = per_minute
        self._key_func = key_func
        self._hits: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001, ANN202
        if scope.get("type") == "http":
            key = self._key_func(scope)
            now = time.monotonic()
            cutoff = now - 60.0
            with self._lock:
                dq = self._hits.get(key)
                if dq is None:
                    dq = self._hits[key] = collections.deque()
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                over = len(dq) >= self._per_minute
                if not over:
                    dq.append(now)
                if len(self._hits) > 20000:  # memory hygiene
                    self._hits = {k: v for k, v in self._hits.items() if v}
            if over:
                resp = JSONResponse(
                    status_code=429,
                    content={
                        "error": "rate_limited",
                        "message": "Too many requests, slow down.",
                    },
                )
                await resp(scope, receive, send)
                return
        await self._app(scope, receive, send)


class PriceRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class BestPriceRequest(BaseModel):
    query: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    upc: str | None = Field(default=None, max_length=32)
    condition: str = Field(default="new", max_length=16)
    country: str = Field(default="US", max_length=8)
    max_results: int = Field(default=10, ge=1, le=50)
    # Broker layer: "direct" (default, plain links) | "broker" (winning
    # offer's link affiliate-tagged when enrolled, disclosed in the
    # response). Validated in find_best_price, not here, so a bad value
    # maps to the route's 422 best_price_failed like other bad inputs.
    path: str = Field(default="direct", max_length=16)


class AffiliateRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    tag: str = Field(min_length=1, max_length=64)
    program: str = Field(default="amazon", min_length=1, max_length=64)


class _BareMcpAsgi:
    """Serve the MCP ASGI app on the bare /mcp path (no trailing slash).

    Starlette's Mount only matches /mcp/*, so the router 307-redirects the
    bare /mcp to /mcp/ — and many MCP clients don't follow redirects. This
    endpoint serves the same ASGI app directly, mirroring the child scope a
    Mount would build (path "/" with root_path extended by "/mcp").
    """

    def __init__(self, mcp_asgi):  # noqa: ANN001, ANN204
        self.mcp_asgi = mcp_asgi
        # SlowAPI's middleware builds a route name from endpoint.__name__;
        # plain instances don't have one, so provide it.
        self.__name__ = "mcp_bare_path"
        self.__module__ = __name__

    async def __call__(self, scope, receive, send):  # noqa: ANN001, ANN202
        bare = dict(scope)
        bare["path"] = "/"
        bare["root_path"] = scope.get("root_path", "") + "/mcp"
        await self.mcp_asgi(bare, receive, send)


def create_app(rate_limit: str | None = None) -> FastAPI:
    # MCP sub-app first: its lifespan must be handed to the parent FastAPI app,
    # otherwise FastMCP's session task group never initializes and every
    # request 500s ("Task group is not initialized"). path="/" serves the MCP
    # endpoint at the mount root, i.e. POST /mcp (the http_app default path
    # would double it to /mcp/mcp).
    mcp_asgi = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)
    mcp_lifespan = mcp_asgi.lifespan  # capture BEFORE wrapping (wrapper has none)
    limit = rate_limit or DEFAULT_RATE_LIMIT
    # H-5: the MCP surface gets its own per-IP rate limiter — SlowAPI's
    # default_limits cannot cover a Starlette Mount (see _McpRateLimitMiddleware).
    mcp_asgi = _McpRateLimitMiddleware(mcp_asgi, _parse_per_minute(limit), _mcp_scope_key)
    app = FastAPI(
        title="price-tracker-mcp",
        version=__version__,
        description=(
            "Price tracking API: check_price (SSRF-guarded) + find_best_price "
            "(multi-source comparison) + make_affiliate_link."
        ),
        lifespan=mcp_lifespan,
    )

    # x402 metering (env-gated, OFF by default) runs outermost.
    warn_if_payments_live()
    app.add_middleware(X402PaymentMiddleware)

    # M-3: proxy-aware key func — one bucket per real client IP, not one
    # shared bucket keyed on Render's load balancer.
    limiter = Limiter(key_func=_rate_limit_key)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "message": "Too many requests, slow down."},
        )

    @app.exception_handler(PriceError)
    async def _price_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=422, content={"error": "price_not_found", "message": str(exc)}
        )

    @app.exception_handler(BestPriceError)
    async def _best_price_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=422, content={"error": "best_price_failed", "message": str(exc)}
        )

    @app.exception_handler(AffiliateError)
    async def _affiliate_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=422, content={"error": "bad_affiliate_link", "message": str(exc)}
        )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "x402": "enabled" if x402_effective_enabled() else "disabled",
            "stripe": "enabled" if stripe_effective_enabled() else "disabled",
        }

    app.include_router(billing_router)

    # Public legal documents (Stripe onboarding URLs). Filled-in legal text,
    # served verbatim — see legal_docs.py. No rate limiting and no x402 charge
    # (EXEMPT_PREFIXES): these must always be reachable.
    @app.get("/legal/terms", include_in_schema=False)
    async def legal_terms():
        return Response(content=TERMS_OF_SERVICE, media_type="text/markdown; charset=utf-8")

    @app.get("/legal/privacy", include_in_schema=False)
    async def legal_privacy():
        return Response(content=PRIVACY_POLICY, media_type="text/markdown; charset=utf-8")

    @app.get("/legal/refunds", include_in_schema=False)
    async def legal_refunds():
        return Response(content=REFUND_POLICY, media_type="text/markdown; charset=utf-8")

    # --- Public support page (directory listings' supportURL target) --------
    # Always free: "/support" is in EXEMPT_PREFIXES in x402.py.
    @app.get("/support", include_in_schema=False)
    async def support_page():
        return Response(content=SUPPORT_PAGE, media_type="text/html; charset=utf-8")

    # --- Public landing / live-demo page (ioconnex.dev links to the root) ---
    # Always free: "/" is in EXEMPT_PREFIXES in x402.py (exact-path match).
    @app.get("/", include_in_schema=False)
    async def landing_page():
        return Response(content=LANDING_PAGE, media_type="text/html; charset=utf-8")

    # --- OpenAI ChatGPT apps domain verification ----------------------------
    # The portal verifies control of this host by fetching the bare token as
    # plain text. The token is issued per plugin draft and supplied via the
    # OPENAI_APPS_CHALLENGE_TOKEN env var (set in Render once the draft
    # exists); 404 until then. The "/.well-known" prefix in EXEMPT_PREFIXES
    # keeps x402 from 402-challenging it.
    @app.get("/.well-known/openai-apps-challenge", include_in_schema=False)
    async def openai_apps_challenge():
        token = os.environ.get("OPENAI_APPS_CHALLENGE_TOKEN", "").strip()
        if not token:
            return Response(status_code=404, content="not found", media_type="text/plain")
        return Response(content=token, media_type="text/plain; charset=utf-8")

    # --- ChatGPT app review demo walkthrough video ---------------------------
    # Public, always free ("/demo-walkthrough.mp4" is in EXEMPT_PREFIXES in
    # x402.py): OpenAI reviewers fetch this URL (the plugin manifest's
    # review.demo_recording_url) during app review. The file ships inside
    # the image at /app/demo-walkthrough.mp4 (DEMO_VIDEO_PATH overrides);
    # 404 when absent so local/dev runs don't need it.
    @app.get("/demo-walkthrough.mp4", include_in_schema=False)
    async def demo_walkthrough():
        video_path = os.environ.get("DEMO_VIDEO_PATH", "/app/demo-walkthrough.mp4")
        if not os.path.isfile(video_path):
            return Response(status_code=404, content="not found", media_type="text/plain")
        return FileResponse(
            video_path, media_type="video/mp4", filename="demo-walkthrough.mp4"
        )

    # --- x402 discovery manifest: public, always free -----------------------
    # No @limiter.limit on this route: discovery must never 429. The
    # "/.well-known" prefix in EXEMPT_PREFIXES keeps the x402 middleware from
    # ever 402-challenging it, even when x402 payments are effectively
    # enabled. Built at request time from build_discovery_manifest() so
    # prices always reflect the live single source of truth (TOOL_PRICE_
    # DEFAULTS + X402_PRICE_*_USDC env overrides).
    @app.get("/.well-known/x402", include_in_schema=False)
    @app.get("/.well-known/x402.json", include_in_schema=False)
    async def x402_discovery_manifest():
        return x402_mod.build_discovery_manifest()

    # --- Glama connector ownership claim: public, always free ----------------
    # HTTP challenge for glama.ai/mcp/connectors/io.github.Anon2052/price-tracker-mcp.
    # Served verbatim; no rate limiting (discovery must never 429) and the
    # "/.well-known" prefix in EXEMPT_PREFIXES keeps x402 from 402-challenging it.
    @app.get("/.well-known/glama.json", include_in_schema=False)
    async def glama_claim():
        return {
            "$schema": "https://glama.ai/mcp/schemas/connector.json",
            "claim": "glama_claim_rZE1zWvHzco25hY6XxC-qESh-btji64O",
        }

    # Always-paid alias (2026-10-24): same handler as /price, but the
    # middleware never free-tiers /paid/* — unpaid callers get the 402.
    @app.post("/paid/price")
    @app.post("/price")
    @limiter.limit(limit)
    async def price(request: Request, body: PriceRequest):  # noqa: ANN001, ANN202
        # H-8: check_price does blocking network I/O (fetch_html) — off the
        # event loop so one slow retailer can't starve every other request.
        return asdict(await asyncio.to_thread(_price.check_price, body.url))

    # Always-paid alias mirrors /paid/price: /paid/* never free-tiers.
    @app.post("/paid/best-price")
    @app.post("/best-price")
    @limiter.limit(limit)
    async def best_price(request: Request, body: BestPriceRequest):  # noqa: ANN001, ANN202
        # find_best_price fans out to retailer APIs (blocking I/O) — off the
        # event loop, same as check_price (H-8).
        return await asyncio.to_thread(
            _bestprice.find_best_price,
            query=body.query,
            url=body.url,
            upc=body.upc,
            condition=body.condition,
            country=body.country,
            max_results=body.max_results,
            path=body.path,
        )

    @app.post("/affiliate")
    @limiter.limit(limit)
    async def affiliate(request: Request, body: AffiliateRequest):  # noqa: ANN001, ANN202
        return {"url": _affiliate.make_affiliate_link(body.url, body.tag, program=body.program)}

    # MCP over streamable HTTP, mounted at /mcp (stateless: safe to mount).
    app.mount("/mcp", mcp_asgi)

    # Bare /mcp must not 307-redirect: serve the MCP app directly (see
    # _BareMcpAsgi). Appended after the mount; the mount's regex can't match
    # the bare path, so this route handles exactly "/mcp".
    app.router.routes.append(
        Route(
            "/mcp",
            _BareMcpAsgi(mcp_asgi),
            methods=["GET", "POST", "DELETE"],
            include_in_schema=False,
        )
    )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "price_tracker.rest_app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
