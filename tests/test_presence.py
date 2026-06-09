"""Presence: derived-presence buckets (pure fn) + heartbeat updates durable fallback."""

from __future__ import annotations

from atmcp import db
from atmcp.config import settings
from atmcp.ids import now_ms
from atmcp.services import identity as identity_svc
from atmcp.services import presence as p
from conftest import join


def test_derive_presence_buckets():
    now = now_ms()
    healthy = {"ts": now - 5_000}      # 5s old
    degraded = {"ts": now - 20_000}    # 20s old
    assert p.derive_presence(healthy, now - 5_000, now) == "healthy"
    assert p.derive_presence(degraded, now - 20_000, now) == "degraded"
    # No heartbeat key (Redis down / expired): fall back to last_seen age.
    assert p.derive_presence(None, now - 5_000, now) == "degraded"
    assert p.derive_presence(None, now - 999_000, now) == "offline"


async def test_heartbeat_updates_status(team):
    a = await join(team, "alice")
    res = await p.heartbeat(a, status_summary="reviewing PR", progress_pct=42)
    assert res["ok"]
    assert res["heartbeat_interval_s"] == settings.heartbeat_interval_s

    row = await db.fetchone(
        "SELECT status_summary, progress_pct FROM agents WHERE team_id=? AND agent_id=?",
        (team["team_id"], a.agent_id),
    )
    assert row["status_summary"] == "reviewing PR"
    assert row["progress_pct"] == 42


async def test_list_agents_roster(team):
    a = await join(team, "alice", caps=["python"])
    b = await join(team, "bob")
    agents = await identity_svc.list_agents(team["team_id"])
    names = {x["display_name"] for x in agents}
    assert names == {"alice", "bob"}
    alice = next(x for x in agents if x["display_name"] == "alice")
    assert alice["capabilities"] == ["python"]
    assert alice["presence"] in {"healthy", "degraded", "offline"}


async def test_rejoin_reuses_agent_id(team):
    a1 = await join(team, "alice", sid="sid-1")
    a2 = await join(team, "alice", sid="sid-2")  # reconnect, new session
    assert a1.agent_id == a2.agent_id
    agents = await identity_svc.list_agents(team["team_id"])
    assert sum(1 for x in agents if x["display_name"] == "alice") == 1
