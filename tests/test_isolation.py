"""Multi-tenant isolation + join-token enforcement."""

from __future__ import annotations

import pytest

from atmcp.services import identity as identity_svc
from atmcp.services import knowledge as k
from atmcp.services import memory as m
from atmcp.services import tasks as t
from conftest import join


async def test_knowledge_and_tasks_are_team_scoped(store):
    team_a = await identity_svc.create_team("alpha")
    team_b = await identity_svc.create_team("beta")
    a = await join(team_a, "a1", sid="sa")
    b = await join(team_b, "b1", sid="sb")

    await k.post_knowledge(a, "secret", "alpha-only finding", tags=["x"])
    await m.set_memory(a, "k", "alpha-value")
    await t.create_task(a, "alpha task")

    # Team B sees none of team A's data.
    assert await k.search_knowledge(b) == []
    assert (await m.get_memory(b, "k"))["found"] is False
    assert await t.list_tasks(team_b["team_id"]) == []

    # Team A sees its own.
    assert len(await k.search_knowledge(a)) == 1
    assert (await m.get_memory(a, "k"))["value"] == "alpha-value"
    assert len(await t.list_tasks(team_a["team_id"])) == 1


async def test_join_requires_valid_token(store):
    team_a = await identity_svc.create_team("alpha")
    with pytest.raises(identity_svc.BadTokenError):
        await identity_svc.join_team("sid-x", "alpha", "wrong-token", "intruder")


async def test_join_unknown_team(store):
    with pytest.raises(identity_svc.UnknownTeamError):
        await identity_svc.join_team("sid-x", "nope", "tok", "ghost")


async def test_duplicate_team_name_rejected(store):
    await identity_svc.create_team("dup")
    with pytest.raises(identity_svc.TeamExistsError):
        await identity_svc.create_team("dup")


async def test_display_name_collision_raises(team):
    await join(team, "alice", sid="s1")
    # A different session/agent_id trying to take 'alice' must be rejected cleanly,
    # not crash with a raw IntegrityError.
    with pytest.raises(identity_svc.DisplayNameTakenError):
        await identity_svc.join_team(
            "s2", team["name"], team["join_token"], "alice", None, "custom-agent-id"
        )
