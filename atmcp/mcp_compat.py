"""ASGI helpers so MCP works behind local HTTP proxies (Clash/V2Ray, etc.).

Some proxies strip ``Content-Type`` on POST or rewrite ``Host`` on forward.
FastMCP transport security then returns HTTP 400/421, which Cursor surfaces as
reconnect failures.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from atmcp.config import settings

_JSON = b"application/json"
_ACCEPT = b"application/json, text/event-stream"


def _canonical_host() -> bytes:
    parsed = urlparse(settings.public_url)
    hostname = parsed.hostname
    if not hostname:
        return b"localhost"
    if parsed.port is not None:
        return f"{hostname}:{parsed.port}".encode()
    return hostname.encode()


def _has_header(headers: list[tuple[bytes, bytes]], name: bytes) -> bool:
    name = name.lower()
    return any(k.lower() == name for k, _ in headers)


def _set_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    name = name.lower()
    filtered = [(k, v) for k, v in headers if k.lower() != name]
    filtered.append((name, value))
    headers[:] = filtered


class McpProxyCompatMiddleware:
    """Restore headers local forward proxies commonly strip or rewrite."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._canonical_host = _canonical_host()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers") or [])
        changed = False

        if scope.get("method") == "POST" and not _has_header(headers, b"content-type"):
            _set_header(headers, b"content-type", _JSON)
            changed = True

        accept = next((v for k, v in headers if k.lower() == b"accept"), b"")
        if b"text/event-stream" not in accept.lower() or b"application/json" not in accept.lower():
            _set_header(headers, b"accept", _ACCEPT)
            changed = True

        # Forward proxies may rewrite Host to a value DNS-rebinding checks reject.
        current_host = next((v for k, v in headers if k.lower() == b"host"), None)
        if current_host != self._canonical_host:
            _set_header(headers, b"host", self._canonical_host)
            changed = True

        if changed:
            scope = dict(scope)
            scope["headers"] = headers
        await self.app(scope, receive, send)
