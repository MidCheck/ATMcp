"""DB-backed idempotency: retries return the stored result and never double-apply,
including for expected-condition (failure/conflict) responses."""

from __future__ import annotations

from atmcp import db
from atmcp.services import knowledge as k
from atmcp.services import memory as m
from atmcp.services import tasks as t
from conftest import join


async def test_create_task_idempotent(team):
    a = await join(team, "alice")
    r1 = await t.create_task(a, "build", idem_key="k1")
    r2 = await t.create_task(a, "build", idem_key="k1")
    assert r1["task_id"] == r2["task_id"]
    count = await db.fetchval("SELECT COUNT(*) FROM tasks WHERE team_id=?", (team["team_id"],))
    assert count == 1  # not duplicated


async def test_post_knowledge_idempotent(team):
    a = await join(team, "alice")
    r1 = await k.post_knowledge(a, "t", "b", idem_key="kk")
    r2 = await k.post_knowledge(a, "t", "b", idem_key="kk")
    assert r1 == r2
    # The retry is a true no-op: no second contribution row.
    c = await db.fetchval(
        "SELECT COUNT(*) FROM knowledge_contributions WHERE team_id=?", (team["team_id"],)
    )
    assert c == 1


async def test_set_memory_idempotent(team):
    a = await join(team, "alice")
    r1 = await m.set_memory(a, "k", 1, idem_key="m1")
    r2 = await m.set_memory(a, "k", 1, idem_key="m1")
    assert r1 == r2
    got = await m.get_memory(a, "k")
    assert got["version"] == 1  # only one write applied


async def test_idem_key_is_scoped_per_agent(team):
    # Two different agents reusing the SAME idem_key must NOT collide.
    a = await join(team, "alice")
    b = await join(team, "bob")
    ra = await t.create_task(a, "alice's task", idem_key="shared")
    rb = await t.create_task(b, "bob's task", idem_key="shared")
    assert ra["task_id"] != rb["task_id"]
    count = await db.fetchval("SELECT COUNT(*) FROM tasks WHERE team_id=?", (team["team_id"],))
    assert count == 2


async def test_claim_failure_result_is_cached(team):
    a = await join(team, "alice")
    b = await join(team, "bob")
    tid = (await t.create_task(a, "x"))["task_id"]
    await t.claim_task(a, tid)  # alice wins
    f1 = await t.claim_task(b, tid, idem_key="bob-claim")
    assert not f1["ok"]
    # Retry with the same idem_key returns the identical stored failure (not re-evaluated).
    f2 = await t.claim_task(b, tid, idem_key="bob-claim")
    assert f2 == f1
