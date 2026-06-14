"""Worker REST API: a non-LLM poller can drive inbox → claim → report over plain HTTP."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from atmcp import web
from atmcp.config import settings


@pytest.fixture
def client(store):
    app = FastAPI()
    web.register(app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _make_team(c):
    team = (await c.post("/api/teams", json={"name": "w"},
                         headers={"X-Admin-Token": settings.admin_token})).json()
    return team["join_token"]


async def test_worker_rest_inbox_claim_report(client):
    async with client as c:
        jt = await _make_team(c)
        auth = {"Authorization": f"Bearer {jt}"}
        # register the worker (creates the roster entry the console can address)
        await c.post("/api/teams/w/heartbeat", json={"display_name": "bob"}, headers=auth)
        # console sends a directive to bob
        did = (await c.post("/api/teams/w/console/command",
                            json={"command": "send bob do the thing", "token": jt})).json()["data"]["directive_id"]

        # bob polls inbox over plain HTTP (no model)
        r = await c.get("/api/teams/w/agents/bob/inbox", headers=auth)
        assert r.status_code == 200
        items = r.json()["directives"]
        assert any(x["directive_id"] == did for x in items)

        # claim + report
        assert (await c.post(f"/api/teams/w/directives/{did}/claim", json={"agent": "bob"}, headers=auth)).json()["ok"]
        rep = await c.post(f"/api/teams/w/directives/{did}/report",
                           json={"agent": "bob", "status": "done", "result_summary": "ok", "output": "the result"},
                           headers=auth)
        assert rep.status_code == 200 and rep.json()["ok"]

        # the issuer sees it done
        det = (await c.get("/api/teams/w/agents/bob/detail")).json()
        d = next(x for x in det["directives"] if x["directive_id"] == did)
        assert d["status"] == "done"


async def test_worker_inbox_requires_token(client):
    async with client as c:
        await _make_team(c)
        assert (await c.get("/api/teams/w/agents/bob/inbox")).status_code == 401


async def test_worker_only_target_can_claim(client):
    async with client as c:
        jt = await _make_team(c)
        auth = {"Authorization": f"Bearer {jt}"}
        await c.post("/api/teams/w/heartbeat", json={"display_name": "bob"}, headers=auth)
        await c.post("/api/teams/w/heartbeat", json={"display_name": "eve"}, headers=auth)
        did = (await c.post("/api/teams/w/console/command",
                            json={"command": "send bob secret task", "token": jt})).json()["data"]["directive_id"]
        # eve tries to claim bob's directive
        r = await c.post(f"/api/teams/w/directives/{did}/claim", json={"agent": "eve"}, headers=auth)
        assert r.json()["ok"] is False and r.json()["error"] == "not_yours"
