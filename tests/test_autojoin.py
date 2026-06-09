"""Auto-join: the first tool call resolves identity from a valid join-token header."""

from __future__ import annotations

import pytest

from atmcp import session


class _FakeReq:
    def __init__(self, headers: dict):
        self.headers = headers  # lowercase keys (Starlette headers are case-insensitive)


class _FakeRC:
    def __init__(self, req):
        self.request = req


class _FakeCtx:
    def __init__(self, headers: dict):
        self.request_context = _FakeRC(_FakeReq(headers))


async def test_auto_join_with_token_and_name(team):
    ctx = _FakeCtx({
        "mcp-session-id": "sid-auto",
        "authorization": f"Bearer {team['join_token']}",
        "x-atmcp-agent": "autobot",
    })
    caller = await session.resolve(ctx)
    assert caller.team_id == team["team_id"]
    assert caller.display_name == "autobot"
    # A second call on the same session returns the same bound identity.
    again = await session.resolve(ctx)
    assert again.agent_id == caller.agent_id


async def test_auto_join_default_name(team):
    ctx = _FakeCtx({
        "mcp-session-id": "sid-deadbeef",
        "authorization": f"Bearer {team['join_token']}",
    })
    caller = await session.resolve(ctx)
    assert caller.display_name.startswith("agent-")


async def test_no_token_raises_not_joined(team):
    ctx = _FakeCtx({"mcp-session-id": "sid-x"})
    with pytest.raises(session.NotJoinedError):
        await session.resolve(ctx)


async def test_bad_token_does_not_join(team):
    ctx = _FakeCtx({"mcp-session-id": "sid-y", "authorization": "Bearer wrong-token"})
    with pytest.raises(session.NotJoinedError):
        await session.resolve(ctx)
