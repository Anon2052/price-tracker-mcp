"""Combined ASGI app: REST API + FastMCP (streamable HTTP) + /health.

Run with:  uvicorn price_tracker.rest_app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.routing import Route

from . import __version__
from . import affiliate as _affiliate
from . import price as _price
from .affiliate import AffiliateError
from .mcp_server import mcp
from .price import PriceError
from .x402 import X402PaymentMiddleware, warn_if_stub_enabled

log = logging.getLogger("price_tracker")

DEFAULT_RATE_LIMIT = os.environ.get("RATE_LIMIT", "60/minute")


class PriceRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


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
    app = FastAPI(
        title="price-tracker-mcp",
        version=__version__,
        description="Price tracking API: check_price (SSRF-guarded) + make_affiliate_link.",
        lifespan=mcp_asgi.lifespan,
    )

    # x402 metering (env-gated, OFF by default) runs outermost.
    warn_if_stub_enabled()
    app.add_middleware(X402PaymentMiddleware)

    limiter = Limiter(key_func=get_remote_address)

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

    @app.exception_handler(AffiliateError)
    async def _affiliate_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=422, content={"error": "bad_affiliate_link", "message": str(exc)}
        )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    limit = rate_limit or DEFAULT_RATE_LIMIT

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "x402": os.environ.get("X402_ENABLED", "false"),
        }

    @app.post("/price")
    @limiter.limit(limit)
    async def price(request: Request, body: PriceRequest):  # noqa: ANN001, ANN202
        return asdict(_price.check_price(body.url))

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
