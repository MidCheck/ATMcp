"""MCP transport security derived from public URL / env overrides."""

from __future__ import annotations

from dataclasses import replace

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request

from atmcp import mcp_transport
from atmcp.config import settings
from atmcp.mcp_transport import build_mcp_transport_security


def test_public_url_allows_lan_host(monkeypatch):
    monkeypatch.setattr(
        mcp_transport,
        "settings",
        replace(settings, public_url="http://192.168.2.7:18000"),
    )
    security = build_mcp_transport_security()
    assert security.enable_dns_rebinding_protection is True
    assert "192.168.2.7:18000" in security.allowed_hosts
    assert "192.168.2.7:*" in security.allowed_hosts
    assert "http://192.168.2.7:18000" in security.allowed_origins


def test_extra_allowed_hosts(monkeypatch):
    monkeypatch.setattr(
        mcp_transport,
        "settings",
        replace(settings, public_url="http://localhost:8000"),
    )
    monkeypatch.setenv("ATMCP_MCP_ALLOWED_HOSTS", "10.0.0.5:9000")
    monkeypatch.setenv("ATMCP_MCP_ALLOWED_ORIGINS", "http://10.0.0.5:9000")
    security = build_mcp_transport_security()
    assert "10.0.0.5:9000" in security.allowed_hosts
    assert "http://10.0.0.5:9000" in security.allowed_origins


def test_dns_rebinding_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ATMCP_MCP_DNS_REBINDING", "0")
    security = build_mcp_transport_security()
    assert security.enable_dns_rebinding_protection is False


@pytest.mark.asyncio
async def test_lan_host_not_rejected(monkeypatch):
    monkeypatch.setattr(
        mcp_transport,
        "settings",
        replace(settings, public_url="http://192.168.2.7:18000"),
    )
    middleware = TransportSecurityMiddleware(build_mcp_transport_security())
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"host", b"192.168.2.7:18000"),
            (b"content-type", b"application/json"),
        ],
    }
    request = Request(scope)
    assert await middleware.validate_request(request, is_post=True) is None
