"""ASGI entrypoint: one FastAPI process mounting the streamable-HTTP MCP server at
/mcp and serving the dashboard + WebSocket. Run with:

    uvicorn atmcp.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request

from atmcp import db, hub, reaper, redis_bus, web
from atmcp.mcp_compat import McpProxyCompatMiddleware
from atmcp.mcp_server import mcp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("atmcp")

# Inner MCP app for lifespan; outer wrapper fixes proxy-stripped headers.
_mcp_inner = mcp.streamable_http_app()
mcp_app = McpProxyCompatMiddleware(_mcp_inner)


def normalize_mcp_scope(scope: dict) -> None:
    if scope.get("type") == "http" and scope.get("path") == "/mcp":
        scope["path"] = "/mcp/"
        scope["raw_path"] = b"/mcp/"


async def _publish(events: list[dict[str, Any]]) -> None:
    """Publisher hook for db.transaction: mirror to Redis (multi-process/catch-up) and
    fan out in-process to dashboard WebSockets + long-poll waiters."""
    await redis_bus.publish_events(events)
    await hub.dispatch(events)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    db.set_publisher(_publish)
    await redis_bus.init()
    await reaper.start()
    log.info("ATMcp ready — MCP at /mcp, dashboard at /dashboard")
    # Adopt the MCP app's lifespan so the StreamableHTTPSessionManager runs.
    async with _mcp_inner.router.lifespan_context(app):
        yield
    await reaper.stop()
    await redis_bus.close()
    await db.close()


app = FastAPI(title="ATMcp — Agent Teams MCP", lifespan=lifespan)


@app.middleware("http")
async def normalize_mcp_path(request: Request, call_next):
    """Accept /mcp (no trailing slash) without a 307 redirect.

    Starlette's Mount redirects bare /mcp -> /mcp/, but many MCP HTTP clients POST
    to the configured URL verbatim and do not follow redirects with the body.
    """
    normalize_mcp_scope(request.scope)
    return await call_next(request)


app.mount("/mcp", mcp_app)
web.register(app)
