"""REST surface: health, admin team creation, and the dashboard snapshot."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from atmcp import web
from atmcp.config import settings
from atmcp.services import identity as identity_svc


@pytest.fixture
def client(store):
    app = FastAPI()
    web.register(app)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health(client):
    async with client as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True


async def test_admin_create_requires_token(client):
    async with client as c:
        r = await c.post("/api/teams", json={"name": "t1"})
        assert r.status_code == 401


async def test_create_team_and_snapshot(client):
    async with client as c:
        r = await c.post(
            "/api/teams",
            json={"name": "webteam"},
            headers={"X-Admin-Token": settings.admin_token},
        )
        assert r.status_code == 200, r.text
        team = r.json()
        assert "join_token" in team and "mcp_url" in team

        # Join an agent (service-level) then read the snapshot.
        await identity_svc.join_team("sid-web", "webteam", team["join_token"], "alice", ["py"])

        r = await c.get("/api/teams/webteam/snapshot")
        assert r.status_code == 200
        snap = r.json()
        assert snap["team"] == "webteam"
        assert any(a["display_name"] == "alice" for a in snap["agents"])
        assert "rollup" in snap and "status" in snap

        r = await c.get("/api/teams/does-not-exist/snapshot")
        assert r.status_code == 404


async def test_rest_heartbeat_creates_and_shows_agent(client):
    async with client as c:
        r = await c.post(
            "/api/teams", json={"name": "hbteam"},
            headers={"X-Admin-Token": settings.admin_token},
        )
        team = r.json()

        # Sidecar heartbeat (REST, join-token auth) registers/refreshes the agent.
        r = await c.post(
            "/api/teams/hbteam/heartbeat",
            json={"display_name": "sidecar-bob", "status_summary": "alive"},
            headers={"Authorization": f"Bearer {team['join_token']}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] and r.json()["agent_id"]

        snap = (await c.get("/api/teams/hbteam/snapshot")).json()
        assert any(a["display_name"] == "sidecar-bob" for a in snap["agents"])

        # Wrong token is rejected.
        r = await c.post(
            "/api/teams/hbteam/heartbeat",
            json={"display_name": "x"},
            headers={"Authorization": "Bearer nope"},
        )
        assert r.status_code == 401


async def test_console_command_and_agent_detail(client):
    async with client as c:
        team = (await c.post(
            "/api/teams", json={"name": "con"},
            headers={"X-Admin-Token": settings.admin_token},
        )).json()
        jt = team["join_token"]
        # register a worker (REST heartbeat) so it's addressable
        await c.post("/api/teams/con/heartbeat", json={"display_name": "bob"},
                     headers={"Authorization": f"Bearer {jt}"})

        # console command without a token → 401
        assert (await c.post("/api/teams/con/console/command", json={"command": "status"})).status_code == 401

        # send a directive via the console (token in body)
        r = await c.post("/api/teams/con/console/command",
                         json={"command": "send bob fix the parser", "token": jt})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["kind"] == "send"
        did = body["data"]["directive_id"]

        # team-wide directives list
        r = await c.get("/api/teams/con/directives")
        assert r.status_code == 200 and r.json()["count"] >= 1
        # filtering by an unknown agent must 404 (not silently return everything)
        assert (await c.get("/api/teams/con/directives?agent=ghost")).status_code == 404

        # agent detail shows the directive addressed to bob
        r = await c.get("/api/teams/con/agents/bob/detail")
        assert r.status_code == 200
        det = r.json()
        assert any(x["directive_id"] == did for x in det["directives"])
        assert det["agent"]["display_name"] == "bob"
