"""MCP proxy compatibility middleware."""

from __future__ import annotations

from atmcp.mcp_compat import McpProxyCompatMiddleware


async def test_injects_content_type_on_post():
    seen: dict = {}

    async def inner(scope, receive, send):
        seen["headers"] = scope.get("headers")

    app = McpProxyCompatMiddleware(inner)
    await app(
        {"type": "http", "method": "POST", "headers": [(b"accept", b"*/*")]},
        None,
        None,
    )
    headers = dict((k.lower(), v) for k, v in seen["headers"])
    assert headers[b"content-type"] == b"application/json"


async def test_leaves_existing_content_type():
    seen: dict = {}

    async def inner(scope, receive, send):
        seen["headers"] = scope.get("headers")

    app = McpProxyCompatMiddleware(inner)
    await app(
        {
            "type": "http",
            "method": "POST",
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        },
        None,
        None,
    )
    headers = dict((k.lower(), v) for k, v in seen["headers"])
    assert headers[b"content-type"] == b"application/json; charset=utf-8"


async def test_normalizes_host_header(monkeypatch):
    monkeypatch.setenv("ATMCP_PUBLIC_URL", "http://192.168.2.7:18000")
    from importlib import reload
    import atmcp.config as config_mod
    import atmcp.mcp_compat as compat_mod

    reload(config_mod)
    reload(compat_mod)

    seen: dict = {}

    async def inner(scope, receive, send):
        seen["headers"] = scope.get("headers")

    app = compat_mod.McpProxyCompatMiddleware(inner)
    await app(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"host", b"127.0.0.1:7890")],
        },
        None,
        None,
    )
    headers = dict((k.lower(), v) for k, v in seen["headers"])
    assert headers[b"host"] == b"192.168.2.7:18000"
