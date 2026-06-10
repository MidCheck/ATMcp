"""MCP mount: /mcp must work without a trailing-slash redirect."""

from __future__ import annotations

from atmcp.app import normalize_mcp_scope


def test_normalize_mcp_scope_rewrites_bare_path():
    scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}
    normalize_mcp_scope(scope)
    assert scope["path"] == "/mcp/"
    assert scope["raw_path"] == b"/mcp/"


def test_normalize_mcp_scope_leaves_other_paths():
    scope = {"type": "http", "path": "/mcp/", "raw_path": b"/mcp/"}
    normalize_mcp_scope(scope)
    assert scope["path"] == "/mcp/"

    scope = {"type": "http", "path": "/dashboard", "raw_path": b"/dashboard"}
    normalize_mcp_scope(scope)
    assert scope["path"] == "/dashboard"
