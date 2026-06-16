"""Token/cost usage accounting: per-agent + team aggregation, rolling windows,
the REST report endpoint, and pruning."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from atmcp import db, web
from atmcp.config import settings
from atmcp.ids import now_ms
from atmcp.services import usage as usage_svc
from tests.conftest import join


async def test_record_and_aggregate(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    alice = await join(team, "alice")

    await usage_svc.record_usage(tid, bob.agent_id, None, "opus", 100, 50, 10, 5, 0.20, 3, 1200)
    await usage_svc.record_usage(tid, bob.agent_id, None, "opus", 200, 80, 0, 0, 0.30, 4, 1500)
    await usage_svc.record_usage(tid, alice.agent_id, None, "sonnet", 40, 20, 0, 0, 0.05, 1, 400)

    u = await usage_svc.team_usage(tid)
    assert u["agents"][bob.agent_id]["input_tokens"] == 300
    assert u["agents"][bob.agent_id]["output_tokens"] == 130
    assert u["agents"][bob.agent_id]["runs"] == 2
    assert round(u["agents"][bob.agent_id]["cost_usd"], 2) == 0.50
    # team totals fold both agents
    assert u["team"]["input_tokens"] == 340
    assert round(u["team"]["cost_usd"], 2) == 0.55
    assert u["team"]["runs"] == 3


async def test_rolling_windows_exclude_old(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    await usage_svc.record_usage(tid, bob.agent_id, None, "opus", 100, 50, 0, 0, 0.10, 1, 100)

    # Backdate one row to 8 days ago: it counts in all-time but not in the 5h/7d windows.
    old_ts = now_ms() - (8 * 24 * 3600 * 1000)
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO usage_events(team_id,agent_id,directive_id,model,input_tokens,"
            "output_tokens,cache_read,cache_creation,cost_usd,num_turns,duration_ms,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, bob.agent_id, None, "opus", 999, 999, 0, 0, 9.99, 1, 1, old_ts),
        )

    u = await usage_svc.team_usage(tid)
    a = u["agents"][bob.agent_id]
    assert a["runs"] == 2                          # all-time sees both
    assert a["window_5h"]["runs"] == 1             # window sees only the recent one
    assert round(a["window_5h"]["cost_usd"], 2) == 0.10
    assert a["window_7d"]["runs"] == 1
    assert round(u["team"]["window_7d"]["cost_usd"], 2) == 0.10


async def test_record_sanitizes_garbage(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    # Negative / non-numeric inputs must clamp to 0, never raise.
    await usage_svc.record_usage(tid, bob.agent_id, None, None, -5, "x", None, None, "nope", None, None)
    u = await usage_svc.team_usage(tid)
    a = u["agents"][bob.agent_id]
    assert a["input_tokens"] == 0 and a["output_tokens"] == 0 and a["cost_usd"] == 0.0


async def test_prune_drops_old_rows(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    await usage_svc.record_usage(tid, bob.agent_id, None, "opus", 10, 10, 0, 0, 0.01, 1, 1)  # recent: kept
    old_ts = now_ms() - (40 * 24 * 3600 * 1000)  # 40 days ago: pruned
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO usage_events(team_id,agent_id,directive_id,model,input_tokens,"
            "output_tokens,cache_read,cache_creation,cost_usd,num_turns,duration_ms,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, bob.agent_id, None, "opus", 99, 99, 0, 0, 9.9, 1, 1, old_ts),
        )
    removed = await usage_svc.prune_expired(30 * 24 * 3600 * 1000)  # keep last 30 days
    assert removed == 1
    u = await usage_svc.team_usage(tid)
    assert u["agents"][bob.agent_id]["runs"] == 1  # only the recent row survives


# ── REST path ───────────────────────────────────────────────────────────────
@pytest.fixture
def client(store):
    app = FastAPI()
    web.register(app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_rest_usage_report_and_snapshot(client):
    async with client as c:
        team = (await c.post("/api/teams", json={"name": "u"},
                             headers={"X-Admin-Token": settings.admin_token})).json()
        jt = team["join_token"]
        auth = {"Authorization": f"Bearer {jt}"}
        await c.post("/api/teams/u/heartbeat", json={"display_name": "bob"}, headers=auth)

        r = await c.post("/api/teams/u/agents/bob/usage", headers=auth, json={
            "model": "opus", "input_tokens": 1000, "output_tokens": 400,
            "cache_read": 200, "cache_creation": 50, "cost_usd": 0.42,
            "num_turns": 5, "duration_ms": 9000,
        })
        assert r.status_code == 200 and r.json()["ok"]

        # appears in the dedicated usage endpoint and the dashboard snapshot
        u = (await c.get("/api/teams/u/usage")).json()
        assert round(u["team"]["cost_usd"], 2) == 0.42
        assert u["team"]["input_tokens"] == 1000
        snap = (await c.get("/api/teams/u/snapshot")).json()
        assert "usage" in snap and round(snap["usage"]["team"]["cost_usd"], 2) == 0.42


async def test_rest_usage_requires_token(client):
    async with client as c:
        await c.post("/api/teams", json={"name": "u"}, headers={"X-Admin-Token": settings.admin_token})
        r = await c.post("/api/teams/u/agents/bob/usage", json={"cost_usd": 1})
        assert r.status_code == 401
