"""Shared fixtures. Tests exercise the service layer directly against a fresh temp
SQLite DB. Redis is intentionally NOT initialized: every redis_bus call is best-effort
and degrades to a no-op, so the core logic is fully testable without Redis running."""

from __future__ import annotations

import pytest
import pytest_asyncio

from atmcp import db, hub, session
from atmcp.services import identity as identity_svc


@pytest_asyncio.fixture
async def store(tmp_path):
    await db.init(str(tmp_path / "atmcp_test.db"))
    db.set_publisher(hub.dispatch)  # events bump the hub generation (wake long-polls)
    try:
        yield
    finally:
        await db.close()
        session._sessions.clear()
        hub._ws_clients.clear()
        hub._conds.clear()
        hub._gen.clear()


@pytest_asyncio.fixture
async def team(store):
    return await identity_svc.create_team("team-A")


async def join(team_rec: dict, display_name: str, caps=None, sid: str | None = None):
    """Join an agent and return its bound Caller."""
    sid = sid or f"sid-{display_name}"
    await identity_svc.join_team(sid, team_rec["name"], team_rec["join_token"], display_name, caps)
    return session._sessions[sid]
