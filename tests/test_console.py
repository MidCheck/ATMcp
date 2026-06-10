"""Server-side /team command runner (dashboard console box)."""

from __future__ import annotations

from atmcp.services import console as c
from atmcp.services import directives as d
from atmcp.services import output as o
from conftest import join


async def test_status_lists_agents(team):
    await join(team, "worker")
    r = await c.run_command(team["team_id"], "dashboard", "status")
    assert r["ok"] and r["kind"] == "status"
    assert any(a["display_name"] == "worker" for a in r["data"]["agents"])


async def test_send_directive_from_console(team):
    worker = await join(team, "worker")
    r = await c.run_command(team["team_id"], "dashboard", "send worker do the thing")
    assert r["ok"] and r["kind"] == "send"
    inb = await d.inbox(worker)
    assert inb["count"] == 1
    assert inb["directives"][0]["from_agent"] == "console:dashboard"


async def test_strips_team_prefix(team):
    r = await c.run_command(team["team_id"], "dashboard", "/team help")
    assert r["kind"] == "help"


async def test_dispatch_creates_task(team):
    r = await c.run_command(team["team_id"], "dashboard", "dispatch build the widget")
    assert r["ok"] and r["data"]["task_id"]


async def test_logs_returns_output(team):
    w = await join(team, "worker")
    await o.append_output(team["team_id"], w.agent_id, "hello from worker")
    r = await c.run_command(team["team_id"], "dashboard", "logs worker")
    assert r["ok"] and r["data"]["count"] == 1
    assert r["data"]["chunks"][0]["text"] == "hello from worker"


async def test_send_to_unknown_agent(team):
    r = await c.run_command(team["team_id"], "dashboard", "send ghost hi")
    assert not r["ok"]


async def test_unknown_command(team):
    r = await c.run_command(team["team_id"], "dashboard", "frobnicate")
    assert not r["ok"] and r["kind"] == "unknown"
