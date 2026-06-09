"""Task coordination: claim race, fencing-token zombie rejection, reaper requeue,
DAG eligibility, and the weighted goal rollup."""

from __future__ import annotations

import asyncio

import pytest

from atmcp import db, reaper
from atmcp.ids import now_ms
from atmcp.services import tasks as t
from conftest import join


async def _expire_lease(team_id: str, task_id: str) -> None:
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE team_id=? AND task_id=?",
            (now_ms() - 1000, team_id, task_id),
        )


async def test_concurrent_claim_single_winner(team):
    a = await join(team, "alice")
    b = await join(team, "bob")
    created = await t.create_task(a, "do the thing")
    tid = created["task_id"]

    r1, r2 = await asyncio.gather(
        t.claim_task(a, tid), t.claim_task(b, tid)
    )
    wins = [r for r in (r1, r2) if r.get("ok")]
    losses = [r for r in (r1, r2) if not r.get("ok")]
    assert len(wins) == 1, (r1, r2)
    assert len(losses) == 1
    assert losses[0]["error"] in {"not_open", "race_lost"}

    board = await t.list_tasks(team["team_id"], status="claimed")
    assert len(board) == 1
    assert board[0]["assignee"] in (a.agent_id, b.agent_id)


async def test_fencing_token_rejects_zombie(team):
    a = await join(team, "alice")
    b = await join(team, "bob")
    tid = (await t.create_task(a, "leased work"))["task_id"]

    claim_a = await t.claim_task(a, tid)
    assert claim_a["ok"]
    ft_a = claim_a["fencing_token"]

    # Agent A "crashes": expire its lease and let the reaper requeue the task.
    await _expire_lease(team["team_id"], tid)
    reaped = await reaper.sweep_once()
    assert reaped == 1

    # Agent B claims the now-open task → new fencing token.
    claim_b = await t.claim_task(b, tid)
    assert claim_b["ok"]
    ft_b = claim_b["fencing_token"]
    assert ft_b > ft_a

    # Zombie A resumes with its stale token → rejected at the DB.
    z = await t.update_task_progress(a, tid, ft_a, progress_pct=50)
    assert z.get("stale_token") is True

    # B (current holder) can progress and complete.
    ok = await t.update_task_progress(b, tid, ft_b, progress_pct=50)
    assert ok["ok"]
    done = await t.complete_task(b, tid, ft_b, "finished")
    assert done["ok"]


async def test_reaper_dead_letters_after_max_attempts(team):
    a = await join(team, "alice")
    tid = (await t.create_task(a, "flaky", lease_ttl_s=90))["task_id"]
    # Force attempts near the limit (default max_attempts=5).
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE tasks SET attempts=4 WHERE team_id=? AND task_id=?", (team["team_id"], tid)
        )
    claim = await t.claim_task(a, tid)
    assert claim["ok"]
    await _expire_lease(team["team_id"], tid)
    await reaper.sweep_once()
    board = await t.list_tasks(team["team_id"])
    assert board[0]["status"] == "failed"  # 5th attempt → dead-letter


async def test_dependency_eligibility(team):
    a = await join(team, "alice")
    first = (await t.create_task(a, "step 1"))["task_id"]
    second = (await t.create_task(a, "step 2", depends_on=[first]))["task_id"]

    # Cannot claim the blocked task.
    blocked = await t.claim_task(a, second)
    assert not blocked["ok"]
    assert blocked["error"] == "deps_unmet"

    # claim_next picks the eligible one (step 1), not the blocked one.
    nxt = await t.claim_next_task(a)
    assert nxt["ok"] and nxt["task_id"] == first
    done = await t.complete_task(a, first, nxt["fencing_token"])
    assert done["ok"]
    assert second in done["eligible_unblocked"]

    # Now step 2 is claimable.
    nxt2 = await t.claim_next_task(a)
    assert nxt2["ok"] and nxt2["task_id"] == second


async def test_priority_order_in_claim_next(team):
    a = await join(team, "alice")
    await t.create_task(a, "low", priority=1)
    hi = (await t.create_task(a, "high", priority=10))["task_id"]
    nxt = await t.claim_next_task(a)
    assert nxt["task_id"] == hi


async def test_goal_rollup_weighted(team):
    a = await join(team, "alice")
    g = (await t.create_goal(a, "ship it"))["goal_id"]
    t1 = (await t.create_task(a, "a", goal_id=g, weight=1))["task_id"]
    t2 = (await t.create_task(a, "b", goal_id=g, weight=3))["task_id"]

    c = await t.claim_task(a, t2)
    await t.complete_task(a, t2, c["fencing_token"])

    roll = await t.goal_rollup(team["team_id"], g)
    assert roll["total_weight"] == 4
    assert roll["done_weight"] == 3
    assert roll["progress_pct"] == 75


async def test_release_requeues(team):
    a = await join(team, "alice")
    tid = (await t.create_task(a, "work"))["task_id"]
    c = await t.claim_task(a, tid)
    rel = await t.release_task(a, tid, c["fencing_token"])
    assert rel["ok"]
    board = await t.list_tasks(team["team_id"])
    assert board[0]["status"] == "open"
    assert board[0]["assignee"] is None
