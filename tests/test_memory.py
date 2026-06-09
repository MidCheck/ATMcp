"""Shared memory: LWW determinism, optimistic CAS conflict-as-data, history."""

from __future__ import annotations

from atmcp import db
from atmcp.services import memory as m
from conftest import join


async def test_last_write_wins(team):
    a = await join(team, "alice")
    b = await join(team, "bob")
    await m.set_memory(a, "leader", "alice")
    await m.set_memory(b, "leader", "bob")
    got = await m.get_memory(a, "leader")
    assert got["found"] and got["value"] == "bob"
    assert got["version"] == 2
    assert got["writer"] == b.agent_id


async def test_lclock_is_monotonic(team):
    a = await join(team, "alice")
    r1 = await m.set_memory(a, "k", 1)
    r2 = await m.set_memory(a, "k", 2)
    assert r2["lclock"] > r1["lclock"]
    assert r2["version"] == r1["version"] + 1


async def test_cas_conflict_surfaces_as_data(team):
    a = await join(team, "alice")
    b = await join(team, "bob")
    await m.set_memory(a, "cfg", {"n": 1})  # version 1

    # Bob's CAS with a stale expected_version → conflict (no write).
    conflict = await m.set_memory(b, "cfg", {"n": 2}, expected_version=0)
    assert conflict.get("conflict") is True
    assert conflict["current_version"] == 1
    assert conflict["current_value"] == {"n": 1}

    # Correct expected_version → applies.
    ok = await m.set_memory(b, "cfg", {"n": 2}, expected_version=1)
    assert ok["ok"] and ok["version"] == 2


async def test_history_records_every_write(team):
    a = await join(team, "alice")
    for i in range(3):
        await m.set_memory(a, "h", i)
    count = await db.fetchval(
        "SELECT COUNT(*) FROM memory_history WHERE team_id=? AND mem_key='h'", (team["team_id"],)
    )
    assert count == 3


async def test_get_all_keys(team):
    a = await join(team, "alice")
    await m.set_memory(a, "x", 1)
    await m.set_memory(a, "y", 2)
    allk = await m.get_memory(a)
    assert set(allk["keys"].keys()) == {"x", "y"}
    assert allk["keys"]["x"]["value"] == 1
