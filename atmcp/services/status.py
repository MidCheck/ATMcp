"""Read-only team snapshot + event catch-up (`sync`)."""

from __future__ import annotations

import json
from typing import Any

from atmcp import db
from atmcp.services import identity as identity_svc
from atmcp.services import tasks as tasks_svc


async def head_event_id(team_id: str) -> int:
    return int(await db.fetchval(
        "SELECT COALESCE(MAX(event_id),0) FROM events WHERE team_id=?", (team_id,)
    ))


async def get_team_status(team_id: str) -> dict[str, Any]:
    agents = await identity_svc.list_agents(team_id)
    online = sum(1 for a in agents if a["presence"] in ("healthy", "degraded"))
    rollup = await tasks_svc.goal_rollup(team_id)
    knowledge_count = int(await db.fetchval(
        "SELECT COUNT(*) FROM knowledge_current WHERE team_id=? AND present=1", (team_id,)
    ))
    return {
        "agents_total": len(agents),
        "agents_online": online,
        "tasks": rollup["by_status"],
        "goal_progress_pct": rollup["progress_pct"],
        "total_weight": rollup["total_weight"],
        "done_weight": rollup["done_weight"],
        "knowledge_count": knowledge_count,
        "head_event_id": await head_event_id(team_id),
    }


def _event_row(r) -> dict[str, Any]:
    return {
        "event_id": r["event_id"],
        "kind": r["kind"],
        "entity_type": r["entity_type"],
        "entity_id": r["entity_id"],
        "actor_agent": r["actor_agent"],
        "payload": json.loads(r["payload_json"]),
        "ts": r["ts"],
    }


async def sync(team_id: str, since_event_id: int = 0, limit: int = 200) -> dict[str, Any]:
    rows = await db.fetchall(
        "SELECT event_id,kind,entity_type,entity_id,actor_agent,payload_json,ts FROM events "
        "WHERE team_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?",
        (team_id, int(since_event_id or 0), max(1, min(int(limit or 200), 1000))),
    )
    evs = [_event_row(r) for r in rows]
    return {"events": evs, "count": len(evs), "head_event_id": await head_event_id(team_id)}


async def recent_events(team_id: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT event_id,kind,entity_type,entity_id,actor_agent,payload_json,ts FROM events "
        "WHERE team_id=? ORDER BY event_id DESC LIMIT ?",
        (team_id, max(1, min(int(limit or 50), 500))),
    )
    return [_event_row(r) for r in rows]
