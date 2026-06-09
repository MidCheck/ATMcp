"""Task coordination: goals, a DAG of tasks, and lease-based claiming.

Safety (no duplicated committed work) is timing-independent: the single-writer lock
plus a conditional `UPDATE … WHERE status='open'` means only one claim wins, and every
mutating call must present the current `fencing_token` (a reaped zombie is rejected at
the DB). Liveness (re-doing abandoned work) is provided by the lease deadline +
reaper. The Redis lease mirrors `lease_expires_at` for fast extension/visibility.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from atmcp import db, events, idempotency, redis_bus
from atmcp.config import settings
from atmcp.db import Tx
from atmcp.ids import new_id, now_ms
from atmcp.session import Caller

_TASK_FIELDS = (
    "task_id, parent_id, goal_id, title, description, status, priority, weight, assignee, "
    "fencing_token, progress_pct, attempts, max_attempts, result_summary, lease_ttl_s, "
    "lease_expires_at, created_at, updated_at, last_event_id"
)


def _task_row(r: aiosqlite.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


async def _deps_unmet(tx: Tx, team_id: str, task_id: str) -> list[str]:
    rows = await tx.fetchall(
        "SELECT d.depends_on FROM task_deps d "
        "LEFT JOIN tasks t ON t.team_id=d.team_id AND t.task_id=d.depends_on "
        "WHERE d.team_id=? AND d.task_id=? AND (t.status IS NULL OR t.status!='done')",
        (team_id, task_id),
    )
    return [r["depends_on"] for r in rows]


async def create_goal(caller: Caller, title: str, description: str | None = None) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    goal_id = new_id()
    now = now_ms()
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO goals(team_id,goal_id,title,description,created_at) VALUES(?,?,?,?,?)",
            (team_id, goal_id, title, description, now),
        )
        eid = await events.append(
            tx, team_id, events.GOAL_CREATED, "goal", goal_id, agent_id, {"title": title}
        )
    return {"ok": True, "goal_id": goal_id, "event_id": eid}


async def create_task(
    caller: Caller,
    title: str,
    description: str | None = None,
    parent_id: str | None = None,
    goal_id: str | None = None,
    priority: int = 0,
    weight: int = 1,
    depends_on: list[str] | None = None,
    lease_ttl_s: int | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    task_id = new_id()
    now = now_ms()
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, idem_key)
            if prior is not None:
                return prior
        await tx.execute(
            "INSERT INTO tasks(team_id,task_id,parent_id,goal_id,title,description,status,priority,"
            "weight,lease_ttl_s,max_attempts,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'open',?,?,?,?,?,?)",
            (team_id, task_id, parent_id, goal_id, title, description, int(priority),
             max(1, int(weight)), lease_ttl_s, settings.task_max_attempts, now, now),
        )
        for dep in depends_on or []:
            await tx.execute(
                "INSERT OR IGNORE INTO task_deps(team_id,task_id,depends_on) VALUES(?,?,?)",
                (team_id, task_id, dep),
            )
        eid = await events.append(
            tx, team_id, events.TASK_CREATED, "task", task_id, agent_id,
            {"title": title, "goal_id": goal_id, "depends_on": depends_on or [],
             "priority": int(priority), "weight": max(1, int(weight))},
        )
        result = {"ok": True, "task_id": task_id, "event_id": eid}
        if idem_key:
            await idempotency.store(tx, team_id, idem_key, result)
    return result


async def _do_claim(tx: Tx, caller: Caller, task_id: str) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    task = await tx.fetchone(
        "SELECT status, assignee, lease_ttl_s FROM tasks WHERE team_id=? AND task_id=?",
        (team_id, task_id),
    )
    if task is None:
        return {"ok": False, "error": "unknown_task"}
    if task["status"] != "open":
        return {"ok": False, "error": "not_open", "status": task["status"], "taken_by": task["assignee"]}
    unmet = await _deps_unmet(tx, team_id, task_id)
    if unmet:
        return {"ok": False, "error": "deps_unmet", "pending_deps": unmet}

    ttl = int(task["lease_ttl_s"] or settings.lease_ttl_s)
    now = now_ms()
    lease_exp = now + ttl * 1000
    cur = await tx.execute(
        "UPDATE tasks SET status='claimed', assignee=?, fencing_token=fencing_token+1, "
        "lease_expires_at=?, updated_at=? WHERE team_id=? AND task_id=? AND status='open'",
        (agent_id, lease_exp, now, team_id, task_id),
    )
    if cur.rowcount != 1:
        t2 = await tx.fetchone(
            "SELECT assignee FROM tasks WHERE team_id=? AND task_id=?", (team_id, task_id)
        )
        return {"ok": False, "error": "race_lost", "taken_by": t2["assignee"] if t2 else None}

    ft = int(await tx.fetchval(
        "SELECT fencing_token FROM tasks WHERE team_id=? AND task_id=?", (team_id, task_id)
    ))
    await tx.execute(
        "INSERT INTO task_claims(team_id,task_id,agent_id,fencing_token,claimed_at) VALUES(?,?,?,?,?)",
        (team_id, task_id, agent_id, ft, now),
    )
    await tx.execute(
        "UPDATE agents SET current_task_id=? WHERE team_id=? AND agent_id=?",
        (task_id, team_id, agent_id),
    )
    await events.append(tx, team_id, events.TASK_CLAIMED, "task", task_id, agent_id,
                        {"fencing_token": ft})
    return {
        "ok": True,
        "task_id": task_id,
        "fencing_token": ft,
        "lease_expires_at": lease_exp,
        "lease_ttl_s": ttl,
    }


async def claim_task(caller: Caller, task_id: str, idem_key: str | None = None) -> dict[str, Any]:
    team_id = caller.team_id
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, idem_key)
            if prior is not None:
                return prior
        result = await _do_claim(tx, caller, task_id)
        if idem_key:
            await idempotency.store(tx, team_id, idem_key, result)
    if result.get("ok"):
        await redis_bus.set_lease(team_id, result["task_id"], caller.agent_id,
                                  result["fencing_token"], result["lease_ttl_s"])
    return result


async def claim_next_task(
    caller: Caller, capability_filter: str | None = None, idem_key: str | None = None
) -> dict[str, Any]:
    team_id = caller.team_id
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, idem_key)
            if prior is not None:
                return prior
        row = await tx.fetchone(
            "SELECT task_id FROM tasks t WHERE t.team_id=? AND t.status='open' "
            "AND NOT EXISTS (SELECT 1 FROM task_deps d "
            "  LEFT JOIN tasks dt ON dt.team_id=d.team_id AND dt.task_id=d.depends_on "
            "  WHERE d.team_id=t.team_id AND d.task_id=t.task_id "
            "  AND (dt.status IS NULL OR dt.status!='done')) "
            "ORDER BY t.priority DESC, t.created_at ASC LIMIT 1",
            (team_id,),
        )
        if row is None:
            result: dict[str, Any] = {"ok": False, "none": True}
        else:
            result = await _do_claim(tx, caller, row["task_id"])
        if idem_key:
            await idempotency.store(tx, team_id, idem_key, result)
    if result.get("ok"):
        await redis_bus.set_lease(team_id, result["task_id"], caller.agent_id,
                                  result["fencing_token"], result["lease_ttl_s"])
    return result


_ACTIVE_STATUS = {"in_progress", "blocked"}


async def update_task_progress(
    caller: Caller,
    task_id: str,
    fencing_token: int,
    progress_pct: int | None = None,
    status: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    ttl = settings.lease_ttl_s
    async with db.transaction() as tx:
        task = await tx.fetchone(
            "SELECT assignee, fencing_token, status, COALESCE(lease_ttl_s,?) AS ttl "
            "FROM tasks WHERE team_id=? AND task_id=?",
            (settings.lease_ttl_s, team_id, task_id),
        )
        if task is None:
            result: dict[str, Any] = {"ok": False, "error": "unknown_task"}
        elif task["assignee"] != agent_id or int(task["fencing_token"]) != int(fencing_token):
            result = {"ok": False, "stale_token": True,
                      "current_fencing_token": task["fencing_token"] if task else None,
                      "current_assignee": task["assignee"] if task else None}
        else:
            ttl = int(task["ttl"])
            new_status = status if status in _ACTIVE_STATUS else (
                "in_progress" if task["status"] == "claimed" else task["status"]
            )
            lease_exp = now + ttl * 1000
            sets = ["status=?", "lease_expires_at=?", "updated_at=?"]
            params: list[Any] = [new_status, lease_exp, now]
            clamped = None
            if progress_pct is not None:
                clamped = max(0, min(100, int(progress_pct)))
                sets.append("progress_pct=?")
                params.append(clamped)
            params += [team_id, task_id]
            await tx.execute(f"UPDATE tasks SET {','.join(sets)} WHERE team_id=? AND task_id=?", params)
            await tx.execute(
                "UPDATE agents SET current_task_id=?, progress_pct=COALESCE(?,progress_pct) "
                "WHERE team_id=? AND agent_id=?",
                (task_id, clamped, team_id, agent_id),
            )
            await events.append(
                tx, team_id, events.TASK_PROGRESS, "task", task_id, agent_id,
                {"progress_pct": clamped, "status": new_status, "note": note},
            )
            result = {"ok": True, "task_id": task_id, "status": new_status, "lease_expires_at": lease_exp}
    if result.get("ok"):
        await redis_bus.extend_lease(team_id, task_id, agent_id, ttl)
    return result


async def complete_task(
    caller: Caller,
    task_id: str,
    fencing_token: int,
    result_summary: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, idem_key)
            if prior is not None:
                return prior
        task = await tx.fetchone(
            "SELECT assignee, fencing_token FROM tasks WHERE team_id=? AND task_id=?",
            (team_id, task_id),
        )
        if task is None:
            result: dict[str, Any] = {"ok": False, "error": "unknown_task"}
        elif task["assignee"] != agent_id or int(task["fencing_token"]) != int(fencing_token):
            result = {"ok": False, "stale_token": True,
                      "current_fencing_token": task["fencing_token"]}
        else:
            await tx.execute(
                "UPDATE tasks SET status='done', progress_pct=100, result_summary=?, "
                "lease_expires_at=NULL, updated_at=? WHERE team_id=? AND task_id=?",
                (result_summary, now, team_id, task_id),
            )
            await tx.execute(
                "UPDATE task_claims SET released_at=?, outcome='done' "
                "WHERE team_id=? AND task_id=? AND agent_id=? AND released_at IS NULL",
                (now, team_id, task_id, agent_id),
            )
            await tx.execute(
                "UPDATE agents SET current_task_id=NULL "
                "WHERE team_id=? AND agent_id=? AND current_task_id=?",
                (team_id, agent_id, task_id),
            )
            eid = await events.append(
                tx, team_id, events.TASK_COMPLETED, "task", task_id, agent_id,
                {"result_summary": result_summary},
            )
            newly = await tx.fetchall(
                "SELECT t.task_id FROM tasks t "
                "JOIN task_deps d ON d.team_id=t.team_id AND d.task_id=t.task_id "
                "WHERE t.team_id=? AND d.depends_on=? AND t.status='open' "
                "AND NOT EXISTS (SELECT 1 FROM task_deps d2 "
                "  LEFT JOIN tasks dt ON dt.team_id=d2.team_id AND dt.task_id=d2.depends_on "
                "  WHERE d2.team_id=t.team_id AND d2.task_id=t.task_id "
                "  AND (dt.status IS NULL OR dt.status!='done'))",
                (team_id, task_id),
            )
            result = {"ok": True, "task_id": task_id, "event_id": eid,
                      "eligible_unblocked": [r["task_id"] for r in newly]}
        if idem_key:
            await idempotency.store(tx, team_id, idem_key, result)
    if result.get("ok"):
        await redis_bus.del_lease(team_id, task_id)
    return result


async def fail_task(
    caller: Caller, task_id: str, fencing_token: int, error: str | None = None
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        task = await tx.fetchone(
            "SELECT assignee, fencing_token, attempts, max_attempts FROM tasks "
            "WHERE team_id=? AND task_id=?",
            (team_id, task_id),
        )
        if task is None:
            result: dict[str, Any] = {"ok": False, "error": "unknown_task"}
        elif task["assignee"] != agent_id or int(task["fencing_token"]) != int(fencing_token):
            result = {"ok": False, "stale_token": True, "current_fencing_token": task["fencing_token"]}
        else:
            attempts = int(task["attempts"]) + 1
            dead = attempts >= int(task["max_attempts"])
            new_status = "failed" if dead else "open"
            await tx.execute(
                "UPDATE tasks SET status=?, assignee=NULL, attempts=?, lease_expires_at=NULL, "
                "updated_at=?, result_summary=? WHERE team_id=? AND task_id=?",
                (new_status, attempts, now, error, team_id, task_id),
            )
            await tx.execute(
                "UPDATE task_claims SET released_at=?, outcome='failed' "
                "WHERE team_id=? AND task_id=? AND agent_id=? AND released_at IS NULL",
                (now, team_id, task_id, agent_id),
            )
            await tx.execute(
                "UPDATE agents SET current_task_id=NULL "
                "WHERE team_id=? AND agent_id=? AND current_task_id=?",
                (team_id, agent_id, task_id),
            )
            kind = events.TASK_FAILED if dead else events.TASK_REQUEUED
            await events.append(tx, team_id, kind, "task", task_id, agent_id,
                                {"error": error, "attempts": attempts, "dead": dead})
            result = {"ok": True, "task_id": task_id, "requeued": not dead, "dead": dead,
                      "attempts": attempts}
    if result.get("ok"):
        await redis_bus.del_lease(team_id, task_id)
    return result


async def release_task(caller: Caller, task_id: str, fencing_token: int) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        task = await tx.fetchone(
            "SELECT assignee, fencing_token FROM tasks WHERE team_id=? AND task_id=?",
            (team_id, task_id),
        )
        if task is None:
            result: dict[str, Any] = {"ok": False, "error": "unknown_task"}
        elif task["assignee"] != agent_id or int(task["fencing_token"]) != int(fencing_token):
            result = {"ok": False, "stale_token": True, "current_fencing_token": task["fencing_token"]}
        else:
            await tx.execute(
                "UPDATE tasks SET status='open', assignee=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE team_id=? AND task_id=?",
                (now, team_id, task_id),
            )
            await tx.execute(
                "UPDATE task_claims SET released_at=?, outcome='released' "
                "WHERE team_id=? AND task_id=? AND agent_id=? AND released_at IS NULL",
                (now, team_id, task_id, agent_id),
            )
            await tx.execute(
                "UPDATE agents SET current_task_id=NULL "
                "WHERE team_id=? AND agent_id=? AND current_task_id=?",
                (team_id, agent_id, task_id),
            )
            await events.append(tx, team_id, events.TASK_RELEASED, "task", task_id, agent_id, {})
            result = {"ok": True, "task_id": task_id}
    if result.get("ok"):
        await redis_bus.del_lease(team_id, task_id)
    return result


async def list_tasks(
    team_id: str,
    status: str | None = None,
    assignee: str | None = None,
    goal_id: str | None = None,
    since_event_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = ["team_id=?"]
    params: list[Any] = [team_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if assignee:
        clauses.append("assignee=?")
        params.append(assignee)
    if goal_id:
        clauses.append("goal_id=?")
        params.append(goal_id)
    if since_event_id:
        clauses.append("last_event_id>?")
        params.append(int(since_event_id))
    params.append(max(1, min(int(limit or 200), 500)))
    rows = await db.fetchall(
        f"SELECT {_TASK_FIELDS} FROM tasks WHERE {' AND '.join(clauses)} "
        "ORDER BY priority DESC, created_at ASC LIMIT ?",
        params,
    )
    return [_task_row(r) for r in rows]


async def goal_rollup(team_id: str, goal_id: str | None = None) -> dict[str, Any]:
    """Headline progress = done_weight / total_weight (from task states, can't drift)."""
    clause = "team_id=?"
    params: list[Any] = [team_id]
    if goal_id:
        clause += " AND goal_id=?"
        params.append(goal_id)
    rows = await db.fetchall(
        f"SELECT status, weight FROM tasks WHERE {clause} AND status!='cancelled'", params
    )
    total = sum(int(r["weight"]) for r in rows)
    done = sum(int(r["weight"]) for r in rows if r["status"] == "done")
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "total_weight": total,
        "done_weight": done,
        "progress_pct": round(100 * done / total) if total else 0,
        "task_count": len(rows),
        "by_status": by_status,
    }
