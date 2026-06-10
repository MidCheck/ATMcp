"""Build MCP streamable-HTTP transport security from runtime settings."""

from __future__ import annotations

import os
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings

from atmcp.config import settings

_LOCALHOST_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOCALHOST_ORIGINS = (
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
)


def _list_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _hosts_from_public_url(public_url: str) -> tuple[list[str], list[str]]:
    parsed = urlparse(public_url)
    hostname = parsed.hostname
    if not hostname:
        return [], []

    scheme = parsed.scheme or "http"
    port = parsed.port
    hosts: list[str] = [f"{hostname}:*"]
    origins: list[str] = [f"{scheme}://{hostname}:*"]
    if port is not None:
        hosts.insert(0, f"{hostname}:{port}")
        origins.insert(0, f"{scheme}://{hostname}:{port}")
    return hosts, origins


def build_mcp_transport_security() -> TransportSecuritySettings:
    """Return FastMCP transport security aligned with how agents reach this server."""
    if os.environ.get("ATMCP_MCP_DNS_REBINDING", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    extra_hosts = _list_env("ATMCP_MCP_ALLOWED_HOSTS")
    extra_origins = _list_env("ATMCP_MCP_ALLOWED_ORIGINS")
    public_hosts, public_origins = _hosts_from_public_url(settings.public_url)

    allowed_hosts = _dedupe([*extra_hosts, *public_hosts, *_LOCALHOST_HOSTS])
    allowed_origins = _dedupe([*extra_origins, *public_origins, *_LOCALHOST_ORIGINS])

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
