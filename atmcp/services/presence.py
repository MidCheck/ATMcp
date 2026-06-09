"""Presence = derived, never stored as truth.

`online` is "heartbeat key exists" (Redis TTL). `heartbeat` also renews any task
lease the agent holds, so a single ~10s call = liveness + lease renewal + progress.
"""

from __future__ import annotations

import json
from typing import Any

from atmcp import db, events, hub, redis_bus
from atmcp.config import settings
from atmcp.ids import new_id, now_ms
from atmcp.session import Caller


def derive_presence(hb: dict[str, Any] | None, last_seen: int, now: int) -> str:
    """healthy (green) / degraded (amber) / offline (grey)."""
    if hb is not None:
        age = (now - int(hb.get("ts", last_seen))) / 1000.0
        if age < settings.presence_healthy_s:
            return "healthy"
        if age < settings.presence_degraded_s:
            return "degraded"
        return "offline"
    # No heartbeat key: either truly offline or Redis is down. Fall back to last_seen.
    age = (now - last_seen) / 1000.0
    return "degraded" if age < settings.presence_degraded_s else "offline"


async def heartbeat(
    caller: Caller,
    status_summary: str | None = None,
    current_task_id: str | None = None,
    progress_pct: int | None = None,
) -> dict[str, Any]:
    now = now_ms()
    team_id, agent_id = caller.team_id, caller.agent_id

    # Update durable presence fallback + self-reported status (no event row: presence
    # is high-frequency and stays out of the audit log).
    async with db.transaction() as tx:
        await tx.execute(
            "UPDATE agents SET last_seen=?, "
            "status_summary=COALESCE(?, status_summary), "
            "current_task_id=COALESCE(?, current_task_id), "
            "progress_pct=COALESCE(?, progress_pct) "
            "WHERE team_id=? AND agent_id=?",
            (now, status_summary, current_task_id, progress_pct, team_id, agent_id),
        )
        # Renew lease deadline (DB authority) for any task this agent holds.
        held = await tx.fetchall(
            "SELECT task_id, COALESCE(lease_ttl_s, ?) AS ttl FROM tasks "
            "WHERE team_id=? AND assignee=? AND status IN ('claimed','in_progress')",
            (settings.lease_ttl_s, team_id, agent_id),
        )
        for t in held:
            await tx.execute(
                "UPDATE tasks SET lease_expires_at=? WHERE team_id=? AND task_id=?",
                (now + int(t["ttl"]) * 1000, team_id, t["task_id"]),
            )

    # Soft state in Redis: heartbeat key (presence) + lease mirror extension.
    hb_payload = {
        "ts": now,
        "status": status_summary,
        "task": current_task_id,
        "progress": progress_pct,
    }
    await redis_bus.set_heartbeat(team_id, agent_id, hb_payload, settings.heartbeat_ttl_s)
    await redis_bus.set_session(caller.session_id, team_id, agent_id, settings.heartbeat_ttl_s * 3)
    for t in held:
        await redis_bus.extend_lease(team_id, t["task_id"], agent_id, int(t["ttl"]))

    # Live presence ping to the dashboard (transient).
    await hub.publish_presence(
        team_id,
        {
            "agent_id": agent_id,
            "presence": "healthy",
            "status": status_summary,
            "current_task": current_task_id,
            "progress_pct": progress_pct,
            "ts": now,
        },
    )
    return {
        "ok": True,
        "server_time": now,
        "heartbeat_interval_s": settings.heartbeat_interval_s,
    }


async def heartbeat_named(
    team_id: str,
    display_name: str,
    status_summary: str | None = None,
    current_task_id: str | None = None,
    progress_pct: int | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Out-of-band heartbeat keyed by display_name (REST sidecar path — no MCP session).

    Resolves/creates the agent by (team, display_name) — the SAME stable identity the MCP
    join uses — so an external timer can keep presence fresh independently of whether the
    LLM happens to call a tool. Decouples liveness from the agent's reasoning loop.
    """
    now = now_ms()
    async with db.transaction() as tx:
        row = await tx.fetchone(
            "SELECT agent_id FROM agents WHERE team_id=? AND display_name=?",
            (team_id, display_name),
        )
        if row is None:
            agent_id = new_id()
            await tx.execute(
                "INSERT INTO agents(team_id,agent_id,display_name,capabilities_json,joined_at,"
                "last_seen,status_summary,current_task_id,progress_pct,retired) "
                "VALUES(?,?,?,?,?,?,?,?,?,0)",
                (team_id, agent_id, display_name, json.dumps(capabilities or []), now, now,
                 status_summary, current_task_id, progress_pct or 0),
            )
            await events.append(
                tx, team_id, events.AGENT_JOINED, "agent", agent_id, agent_id,
                {"display_name": display_name, "via": "rest_heartbeat"},
            )
        else:
            agent_id = row["agent_id"]
            await tx.execute(
                "UPDATE agents SET last_seen=?, retired=0, "
                "status_summary=COALESCE(?, status_summary), "
                "current_task_id=COALESCE(?, current_task_id), "
                "progress_pct=COALESCE(?, progress_pct) "
                "WHERE team_id=? AND agent_id=?",
                (now, status_summary, current_task_id, progress_pct, team_id, agent_id),
            )
        held = await tx.fetchall(
            "SELECT task_id, COALESCE(lease_ttl_s, ?) AS ttl FROM tasks "
            "WHERE team_id=? AND assignee=? AND status IN ('claimed','in_progress')",
            (settings.lease_ttl_s, team_id, agent_id),
        )
        for t in held:
            await tx.execute(
                "UPDATE tasks SET lease_expires_at=? WHERE team_id=? AND task_id=?",
                (now + int(t["ttl"]) * 1000, team_id, t["task_id"]),
            )

    await redis_bus.set_heartbeat(
        team_id, agent_id,
        {"ts": now, "status": status_summary, "task": current_task_id, "progress": progress_pct},
        settings.heartbeat_ttl_s,
    )
    for t in held:
        await redis_bus.extend_lease(team_id, t["task_id"], agent_id, int(t["ttl"]))
    await hub.publish_presence(
        team_id,
        {"agent_id": agent_id, "presence": "healthy", "status": status_summary,
         "current_task": current_task_id, "progress_pct": progress_pct, "ts": now},
    )
    return {
        "ok": True,
        "agent_id": agent_id,
        "server_time": now,
        "heartbeat_interval_s": settings.heartbeat_interval_s,
    }
