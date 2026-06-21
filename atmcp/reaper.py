"""Lease-expiry reaper: the crash-recovery mechanism.

Every reaper_interval_s it re-queues tasks whose lease deadline has passed (the agent
stopped heartbeating). Fencing tokens make this safe: a zombie that resumes after being
reaped presents a stale token and is rejected at the DB, so re-queuing only ever affects
liveness, never correctness. A task that exceeds max_attempts is dead-lettered.
"""

from __future__ import annotations

import asyncio
import logging

from atmcp import db, events, idempotency, redis_bus
from atmcp.config import settings
from atmcp.ids import now_ms
from atmcp.services import guard as guard_svc
from atmcp.services import output as output_svc
from atmcp.services import usage as usage_svc

log = logging.getLogger("atmcp.reaper")

_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def sweep_once() -> int:
    now = now_ms()
    candidates = await db.fetchall(
        "SELECT team_id, task_id FROM tasks "
        "WHERE status IN ('claimed','in_progress') AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at < ?",
        (now,),
    )
    reaped = 0
    for c in candidates:
        team_id, task_id = c["team_id"], c["task_id"]
        async with db.transaction() as tx:
            t = await tx.fetchone(
                "SELECT status, lease_expires_at, assignee, attempts, max_attempts "
                "FROM tasks WHERE team_id=? AND task_id=?",
                (team_id, task_id),
            )
            if t is None or t["status"] not in ("claimed", "in_progress"):
                continue
            if t["lease_expires_at"] is None or int(t["lease_expires_at"]) >= now_ms():
                continue  # heartbeat renewed it between scan and lock
            attempts = int(t["attempts"]) + 1
            dead = attempts >= int(t["max_attempts"])
            new_status = "failed" if dead else "open"
            ts = now_ms()
            await tx.execute(
                "UPDATE tasks SET status=?, assignee=NULL, attempts=?, lease_expires_at=NULL, "
                "updated_at=? WHERE team_id=? AND task_id=?",
                (new_status, attempts, ts, team_id, task_id),
            )
            await tx.execute(
                "UPDATE task_claims SET released_at=?, outcome='expired' "
                "WHERE team_id=? AND task_id=? AND agent_id=? AND released_at IS NULL",
                (ts, team_id, task_id, t["assignee"]),
            )
            await tx.execute(
                "UPDATE agents SET current_task_id=NULL "
                "WHERE team_id=? AND agent_id=? AND current_task_id=?",
                (team_id, t["assignee"], task_id),
            )
            kind = events.TASK_FAILED if dead else events.TASK_REQUEUED
            await events.append(
                tx, team_id, kind, "task", task_id, t["assignee"],
                {"reason": "lease_expired", "attempts": attempts, "dead": dead},
            )
        await redis_bus.del_lease(team_id, task_id)
        reaped += 1
        log.info("reaped task %s (team %s): lease expired -> %s", task_id, team_id, new_status)
    return reaped


async def _loop() -> None:
    while not _stop.is_set():
        try:
            await sweep_once()
            await idempotency.prune_expired(settings.idem_ttl_s * 1000)
            await output_svc.prune_expired(settings.output_retention_s * 1000)
            await usage_svc.prune_expired(settings.usage_retention_s * 1000)
            await guard_svc.prune_expired(settings.guard_retention_s * 1000)
        except Exception as exc:  # noqa: BLE001
            log.warning("reaper sweep error: %s", exc)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=settings.reaper_interval_s)
        except asyncio.TimeoutError:
            pass


async def start() -> None:
    global _task
    _stop.clear()
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    _stop.set()
    if _task is not None:
        try:
            await _task
        except Exception:  # noqa: BLE001
            pass
