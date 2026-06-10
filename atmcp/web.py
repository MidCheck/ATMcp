"""FastAPI surface: live dashboard, JSON snapshot API, WebSocket fan-out, health, and
the admin team-creation endpoint. Dashboard auth is OFF by default (open access);
flip ATMCP_DASHBOARD_AUTH=1 to require a per-team read-only token.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from atmcp import db, hub, redis_bus
from atmcp.config import settings
from atmcp.ids import hash_token, token_matches
from atmcp.services import console as console_svc
from atmcp.services import directives as directives_svc
from atmcp.services import identity as identity_svc
from atmcp.services import output as output_svc
from atmcp.services import presence as presence_svc
from atmcp.services import status as status_svc
from atmcp.services import tasks as tasks_svc

log = logging.getLogger("atmcp.web")
STATIC_DIR = Path(__file__).parent / "static"


class _LockedWS:
    """Serializes sends on one WebSocket so the backfill loop and concurrent hub
    broadcasts (from committed transactions) can never interleave a send_json()."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, data: Any) -> None:
        async with self._lock:
            await self._ws.send_json(data)


async def _team_by_name(name: str):
    return await db.fetchone(
        "SELECT team_id, name, dashboard_token_hash FROM teams WHERE name=?", (name,)
    )


def _bearer(value: str | None) -> str | None:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def _require_admin(authorization: str | None, x_admin_token: str | None) -> None:
    token = _bearer(authorization) or x_admin_token
    if not token or not token_matches(token, hash_token(settings.admin_token)):
        raise HTTPException(status_code=401, detail="admin token required")


def _check_dashboard(team_row, token: str | None) -> None:
    if not settings.dashboard_auth:
        return
    h = team_row["dashboard_token_hash"]
    if not (token and h and token_matches(token, h)):
        raise HTTPException(status_code=401, detail="dashboard token required")


async def _snapshot(team_id: str, name: str) -> dict[str, Any]:
    agents = await identity_svc.list_agents(team_id)
    board = await tasks_svc.list_tasks(team_id, limit=500)
    rollup = await tasks_svc.goal_rollup(team_id)
    st = await status_svc.get_team_status(team_id)
    feed = await status_svc.recent_events(team_id, limit=80)
    goals = await db.fetchall(
        "SELECT goal_id, title, description, created_at FROM goals WHERE team_id=? ORDER BY created_at",
        (team_id,),
    )
    knowledge = await db.fetchall(
        "SELECT content_id, title, tags_json, first_author, contributor_count, last_seen_at "
        "FROM knowledge_current WHERE team_id=? AND present=1 ORDER BY last_seen_at DESC LIMIT 50",
        (team_id,),
    )
    return {
        "team": name,
        "team_id": team_id,
        "status": st,
        "rollup": rollup,
        "agents": agents,
        "tasks": board,
        "goals": [dict(g) for g in goals],
        "knowledge": [dict(k) for k in knowledge],
        "events": feed,
        "head_event_id": st["head_event_id"],
    }


def register(app: FastAPI) -> None:
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "atmcp"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await db.fetchval("SELECT 1")
            sqlite_ok = True
        except Exception:  # noqa: BLE001
            sqlite_ok = False
        redis_ok = await redis_bus.ping()
        code = 200 if sqlite_ok else 503
        return JSONResponse({"sqlite": sqlite_ok, "redis": redis_ok}, status_code=code)

    @app.post("/api/teams")
    async def create_team(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_admin(authorization, x_admin_token)
        name = (body or {}).get("name")
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        try:
            return await identity_svc.create_team(
                name, body.get("join_token"), body.get("dashboard_token")
            )
        except identity_svc.TeamExistsError:
            raise HTTPException(status_code=409, detail=f"team exists: {name}")

    @app.get("/api/teams/{team_name}/snapshot")
    async def snapshot(team_name: str, token: str | None = None) -> dict[str, Any]:
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        _check_dashboard(team, token)
        return await _snapshot(team["team_id"], team["name"])

    @app.get("/api/teams/{team_name}/status")
    async def team_status(team_name: str, token: str | None = None) -> dict[str, Any]:
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        _check_dashboard(team, token)
        return await status_svc.get_team_status(team["team_id"])

    # ── dashboard console + agent drill-down ───────────────────────────────
    @app.post("/api/teams/{team_name}/console/command")
    async def console_command(
        team_name: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
        x_atmcp_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Run a /team command from the dashboard console box. Auth = the join token
        (write-capable; the dashboard sends it in a header or the body)."""
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        token = _bearer(authorization) or x_atmcp_token or (body or {}).get("token")
        join_hash = await db.fetchval(
            "SELECT join_token_hash FROM teams WHERE team_id=?", (team["team_id"],)
        )
        if not token or not join_hash or not token_matches(token, join_hash):
            raise HTTPException(status_code=401, detail="invalid join token")
        return await console_svc.run_command(
            team["team_id"], (body or {}).get("console") or "dashboard", (body or {}).get("command", "")
        )

    @app.get("/api/teams/{team_name}/directives")
    async def list_team_directives(
        team_name: str, status: str | None = None, agent: str | None = None,
        limit: int = 100, token: str | None = None,
    ) -> dict[str, Any]:
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        _check_dashboard(team, token)
        to_agent = None
        if agent:
            to_agent = await identity_svc.resolve_agent_ref(team["team_id"], agent)
            if to_agent is None:
                raise HTTPException(status_code=404, detail="unknown agent")
        items = await directives_svc.list_team(team["team_id"], status, to_agent, limit)
        return {"directives": items, "count": len(items)}

    @app.get("/api/teams/{team_name}/agents/{agent_ref}/detail")
    async def agent_detail(team_name: str, agent_ref: str, token: str | None = None) -> dict[str, Any]:
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        _check_dashboard(team, token)
        aid = await identity_svc.resolve_agent_ref(team["team_id"], agent_ref)
        if aid is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        agents = await identity_svc.list_agents(team["team_id"])
        me = next((a for a in agents if a["agent_id"] == aid), None)
        directives = await directives_svc.list_team(team["team_id"], to_agent=aid, limit=50)
        tasks = await tasks_svc.list_tasks(team["team_id"], assignee=aid, limit=100)
        out = await output_svc.get_output(team["team_id"], aid, 0, 0, 300)
        return {
            "agent": me, "agent_id": aid, "directives": directives, "tasks": tasks,
            "output": out["chunks"], "head_seq": out["head_seq"],
        }

    @app.get("/api/teams/{team_name}/agents/{agent_ref}/output")
    async def agent_output_get(
        team_name: str, agent_ref: str, since_seq: int = 0, limit: int = 200, token: str | None = None
    ) -> dict[str, Any]:
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        _check_dashboard(team, token)
        aid = await identity_svc.resolve_agent_ref(team["team_id"], agent_ref)
        if aid is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        return await output_svc.get_output(team["team_id"], aid, since_seq, 0, limit)

    @app.post("/api/teams/{team_name}/heartbeat")
    async def rest_heartbeat(
        team_name: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
        x_atmcp_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Out-of-band presence: a sidecar/cron/hook can keep an agent 'online' on a timer
        without the LLM having to call the MCP heartbeat tool. Auth = the team join token."""
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        token = _bearer(authorization) or x_atmcp_token
        join_hash = await db.fetchval(
            "SELECT join_token_hash FROM teams WHERE team_id=?", (team["team_id"],)
        )
        if not token or not join_hash or not token_matches(token, join_hash):
            raise HTTPException(status_code=401, detail="invalid join token")
        display_name = (body or {}).get("display_name")
        if not display_name:
            raise HTTPException(status_code=400, detail="display_name required")
        return await presence_svc.heartbeat_named(
            team["team_id"],
            display_name,
            (body or {}).get("status_summary"),
            (body or {}).get("current_task_id"),
            (body or {}).get("progress_pct"),
            (body or {}).get("capabilities"),
        )

    @app.post("/api/teams/{team_name}/agents/{agent_ref}/output")
    async def rest_output(
        team_name: str,
        agent_ref: str,
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
        x_atmcp_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Out-of-band output capture (e.g. a Claude Code hook shipping its transcript tail).
        Auth = the team join token; agent_ref is a display_name or agent_id."""
        team = await _team_by_name(team_name)
        if team is None:
            raise HTTPException(status_code=404, detail="unknown team")
        token = _bearer(authorization) or x_atmcp_token
        join_hash = await db.fetchval(
            "SELECT join_token_hash FROM teams WHERE team_id=?", (team["team_id"],)
        )
        if not token or not join_hash or not token_matches(token, join_hash):
            raise HTTPException(status_code=401, detail="invalid join token")
        agent_id = await identity_svc.resolve_agent_ref(team["team_id"], agent_ref)
        if agent_id is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        text = (body or {}).get("text")
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        return await output_svc.append_output(
            team["team_id"], agent_id, text, (body or {}).get("directive_id"), source="hook"
        )

    @app.websocket("/ws/{team_name}")
    async def ws(websocket: WebSocket, team_name: str) -> None:
        team = await _team_by_name(team_name)
        if team is None:
            await websocket.close(code=4404)
            return
        token = websocket.query_params.get("token")
        if settings.dashboard_auth:
            h = team["dashboard_token_hash"]
            if not (token and h and token_matches(token, h)):
                await websocket.close(code=4401)
                return

        team_id = team["team_id"]
        await websocket.accept()
        sender = _LockedWS(websocket)
        hub.register_ws(team_id, sender)
        try:
            since = int(websocket.query_params.get("since_event_id") or 0)
            backfill = await status_svc.sync(team_id, since, 1000)
            await sender.send_json({"type": "hello", "head_event_id": backfill["head_event_id"]})
            for ev in backfill["events"]:
                await sender.send_json({"type": "event", **ev})
            while True:  # keep the socket open; ignore inbound (client pings)
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("ws closed: %s", exc)
        finally:
            hub.unregister_ws(team_id, sender)

    @app.get("/dashboard")
    async def dashboard() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "dashboard.html"))

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "service": "ATMcp — Agent Teams MCP",
            "mcp_url": f"{settings.public_url.rstrip('/')}/mcp",
            "dashboard": f"{settings.public_url.rstrip('/')}/dashboard?team=<team>",
        }
