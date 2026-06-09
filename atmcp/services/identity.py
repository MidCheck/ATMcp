"""Team creation (admin) and agent identity: join / leave / roster."""

from __future__ import annotations

import json
from typing import Any

from atmcp import db, events, redis_bus
from atmcp.config import settings
from atmcp.ids import gen_token, hash_token, new_id, now_ms, token_matches
from atmcp.services.presence import derive_presence
from atmcp.session import Caller, bind, unbind


class TeamExistsError(Exception):
    pass


class UnknownTeamError(Exception):
    pass


class BadTokenError(Exception):
    pass


class DisplayNameTakenError(Exception):
    """display_name already used by a different agent in the team (UNIQUE per team)."""


async def create_team(
    name: str, join_token: str | None = None, dashboard_token: str | None = None
) -> dict[str, Any]:
    """Admin op. Returns the plaintext tokens ONCE (only hashes are stored)."""
    join_token = join_token or gen_token()
    dashboard_token = dashboard_token or gen_token()
    team_id = new_id()
    async with db.transaction() as tx:
        existing = await tx.fetchone("SELECT team_id FROM teams WHERE name=?", (name,))
        if existing is not None:
            raise TeamExistsError(name)
        await tx.execute(
            "INSERT INTO teams(team_id,name,join_token_hash,dashboard_token_hash,created_at) "
            "VALUES(?,?,?,?,?)",
            (team_id, name, hash_token(join_token), hash_token(dashboard_token), now_ms()),
        )
    return {
        "team_id": team_id,
        "name": name,
        "join_token": join_token,
        "dashboard_token": dashboard_token,
        "mcp_url": f"{settings.public_url.rstrip('/')}/mcp",
        "dashboard_url": f"{settings.public_url.rstrip('/')}/dashboard?team={name}",
    }


async def join_team(
    sid: str,
    team_name: str,
    join_token: str,
    display_name: str,
    capabilities: list[str] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    team = await db.fetchone(
        "SELECT team_id, join_token_hash FROM teams WHERE name=?", (team_name,)
    )
    if team is None:
        raise UnknownTeamError(team_name)
    if not token_matches(join_token, team["join_token_hash"]):
        raise BadTokenError()

    team_id = team["team_id"]
    caps_json = json.dumps(capabilities or [])
    now = now_ms()

    async with db.transaction() as tx:
        if agent_id:
            existing = await tx.fetchone(
                "SELECT agent_id FROM agents WHERE team_id=? AND agent_id=?", (team_id, agent_id)
            )
        else:
            existing = await tx.fetchone(
                "SELECT agent_id FROM agents WHERE team_id=? AND display_name=?",
                (team_id, display_name),
            )
        # Guard UNIQUE(team_id, display_name): the name must not already belong to a
        # *different* agent (atomic under the single-writer lock — no TOCTOU).
        name_owner = await tx.fetchone(
            "SELECT agent_id FROM agents WHERE team_id=? AND display_name=?",
            (team_id, display_name),
        )
        if existing is not None:
            aid = existing["agent_id"]
            if name_owner is not None and name_owner["agent_id"] != aid:
                raise DisplayNameTakenError(display_name)
            await tx.execute(
                "UPDATE agents SET display_name=?, capabilities_json=?, session_id=?, "
                "last_seen=?, retired=0, status_summary='joined' "
                "WHERE team_id=? AND agent_id=?",
                (display_name, caps_json, sid, now, team_id, aid),
            )
            reused = True
        else:
            if name_owner is not None:
                raise DisplayNameTakenError(display_name)
            aid = agent_id or new_id()
            await tx.execute(
                "INSERT INTO agents(team_id,agent_id,display_name,capabilities_json,session_id,"
                "joined_at,last_seen,status_summary,progress_pct,retired) "
                "VALUES(?,?,?,?,?,?,?,?,0,0)",
                (team_id, aid, display_name, caps_json, sid, now, now, "joined"),
            )
            reused = False
        eid = await events.append(
            tx,
            team_id,
            events.AGENT_JOINED,
            "agent",
            aid,
            aid,
            {"display_name": display_name, "capabilities": capabilities or [], "reused": reused},
        )

    await redis_bus.set_heartbeat(
        team_id, aid, {"ts": now, "status": "joined", "task": None, "progress": 0},
        settings.heartbeat_ttl_s,
    )
    await redis_bus.set_session(sid, team_id, aid, settings.heartbeat_ttl_s * 3)
    bind(sid, team_id, aid, display_name)

    return {
        "agent_id": aid,
        "team_id": team_id,
        "display_name": display_name,
        "reused_existing": reused,
        "head_event_id": eid,
        "heartbeat_interval_s": settings.heartbeat_interval_s,
        "lease_ttl_s": settings.lease_ttl_s,
    }


async def leave_team(caller: Caller) -> dict[str, Any]:
    now = now_ms()
    requeued: list[str] = []
    async with db.transaction() as tx:
        held = await tx.fetchall(
            "SELECT task_id FROM tasks WHERE team_id=? AND assignee=? "
            "AND status IN ('claimed','in_progress')",
            (caller.team_id, caller.agent_id),
        )
        for t in held:
            await tx.execute(
                "UPDATE tasks SET status='open', assignee=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE team_id=? AND task_id=?",
                (now, caller.team_id, t["task_id"]),
            )
            await tx.execute(
                "UPDATE task_claims SET released_at=?, outcome='released' "
                "WHERE team_id=? AND task_id=? AND agent_id=? AND released_at IS NULL",
                (now, caller.team_id, t["task_id"], caller.agent_id),
            )
            await events.append(
                tx, caller.team_id, events.TASK_REQUEUED, "task", t["task_id"],
                caller.agent_id, {"reason": "agent_left"},
            )
            requeued.append(t["task_id"])
        await tx.execute(
            "UPDATE agents SET status_summary='left', current_task_id=NULL, session_id=NULL, "
            "last_seen=? WHERE team_id=? AND agent_id=?",
            (now, caller.team_id, caller.agent_id),
        )
        await events.append(
            tx, caller.team_id, events.AGENT_LEFT, "agent", caller.agent_id, caller.agent_id, {}
        )

    for tid in requeued:
        await redis_bus.del_lease(caller.team_id, tid)
    await redis_bus.del_heartbeat(caller.team_id, caller.agent_id)
    await redis_bus.del_session(caller.session_id)
    unbind(caller.session_id)
    return {"ok": True, "requeued_tasks": requeued}


async def list_agents(team_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT agent_id, display_name, capabilities_json, joined_at, last_seen, "
        "status_summary, current_task_id, progress_pct, retired "
        "FROM agents WHERE team_id=? ORDER BY joined_at",
        (team_id,),
    )
    ids = [r["agent_id"] for r in rows]
    hbs = await redis_bus.get_heartbeats(team_id, ids)
    now = now_ms()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "agent_id": r["agent_id"],
                "display_name": r["display_name"],
                "capabilities": json.loads(r["capabilities_json"]),
                "presence": derive_presence(hbs.get(r["agent_id"]), r["last_seen"], now),
                "status_summary": r["status_summary"],
                "current_task_id": r["current_task_id"],
                "progress_pct": r["progress_pct"],
                "last_seen": r["last_seen"],
                "joined_at": r["joined_at"],
            }
        )
    return out
