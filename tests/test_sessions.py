"""Workbench sessions (threads): CRUD, session-scoped directives/output, isolation,
backward compatibility (NULL session_id = default thread), and the REST surface."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from atmcp import web
from atmcp.config import settings
from atmcp.services import directives as directives_svc
from atmcp.services import output as output_svc
from atmcp.services import sessions as sessions_svc
from atmcp.session import Caller
from tests.conftest import join


async def test_create_list_get_rename_archive(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    s = await sessions_svc.create_session(tid, bob.agent_id, "fix parser", driver="claude")
    sid = s["session_id"]
    assert s["title"] == "fix parser" and s["status"] == "active"

    lst = await sessions_svc.list_sessions(tid, bob.agent_id)
    assert [x["session_id"] for x in lst] == [sid]

    assert (await sessions_svc.rename_session(tid, sid, "parser v2"))["ok"]
    assert (await sessions_svc.get_session(tid, sid))["title"] == "parser v2"

    assert (await sessions_svc.archive_session(tid, sid))["ok"]
    assert await sessions_svc.list_sessions(tid, bob.agent_id) == []           # archived hidden
    assert len(await sessions_svc.list_sessions(tid, bob.agent_id, include_archived=True)) == 1


async def test_directive_and_output_are_session_scoped(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    console = Caller(tid, "console:c", "c", "rest-console:c")
    s1 = (await sessions_svc.create_session(tid, bob.agent_id, "t1"))["session_id"]
    s2 = (await sessions_svc.create_session(tid, bob.agent_id, "t2"))["session_id"]

    r = await directives_svc.send_directive(console, "bob", "do A", session_id=s1)
    assert r["ok"] and r["session_id"] == s1
    await output_svc.append_output(tid, bob.agent_id, "A output", directive_id=r["directive_id"], session_id=s1)
    await output_svc.append_output(tid, bob.agent_id, "B output", session_id=s2)

    # bob's inbox carries the session id so the worker knows the thread
    inbox = await directives_svc.inbox(bob)
    assert any(d["directive_id"] == r["directive_id"] and d["session_id"] == s1
               for d in inbox["directives"])

    # each thread sees only its own output
    o1 = await sessions_svc.session_output(tid, s1)
    o2 = await sessions_svc.session_output(tid, s2)
    assert [c["text"] for c in o1["chunks"]] == ["A output"]
    assert [c["text"] for c in o2["chunks"]] == ["B output"]

    # transcript bundles the thread's user message(s) + output
    tr = await sessions_svc.transcript(tid, s1)
    assert tr["directives"][0]["instruction"] == "do A"
    assert tr["output"][0]["text"] == "A output"


async def test_foreign_or_unknown_session_drops_to_default_thread(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    alice = await join(team, "alice")
    console = Caller(tid, "console:c", "c", "rest-console:c")
    s_alice = (await sessions_svc.create_session(tid, alice.agent_id, "a"))["session_id"]

    # a session that belongs to a DIFFERENT agent must not attach to bob's directive
    r1 = await directives_svc.send_directive(console, "bob", "x", session_id=s_alice)
    assert r1["ok"] and r1["session_id"] is None
    # a session id that doesn't exist at all → NULL too
    r2 = await directives_svc.send_directive(console, "bob", "y", session_id="nope")
    assert r2["ok"] and r2["session_id"] is None


async def test_legacy_directive_without_session_still_works(team):
    # /team-style send (no session) keeps working unchanged — the default thread.
    tid = team["team_id"]
    await join(team, "bob")
    console = Caller(tid, "console:c", "c", "rest-console:c")
    r = await directives_svc.send_directive(console, "bob", "legacy")
    assert r["ok"] and r["session_id"] is None


# ── REST surface ─────────────────────────────────────────────────────────────
@pytest.fixture
def client(store):
    app = FastAPI()
    web.register(app)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_rest_session_flow(client):
    async with client as c:
        team = (await c.post("/api/teams", json={"name": "wb"},
                             headers={"X-Admin-Token": settings.admin_token})).json()
        jt = team["join_token"]
        auth = {"Authorization": f"Bearer {jt}"}
        await c.post("/api/teams/wb/heartbeat", json={"display_name": "bob"}, headers=auth)

        # create a thread under bob
        sid = (await c.post("/api/teams/wb/sessions", json={"agent": "bob", "title": "chat 1"},
                            headers=auth)).json()["session_id"]
        assert (await c.get("/api/teams/wb/sessions?agent=bob")).json()["count"] == 1

        # send a chat message → a session-scoped directive
        msg = await c.post(f"/api/teams/wb/sessions/{sid}/message", json={"text": "hello bob"}, headers=auth)
        assert msg.status_code == 200 and msg.json()["session_id"] == sid

        # the transcript shows the user message
        got = (await c.get(f"/api/teams/wb/sessions/{sid}")).json()
        assert got["session"]["title"] == "chat 1"
        assert got["directives"][0]["instruction"] == "hello bob"

        # rename + archive
        assert (await c.post(f"/api/teams/wb/sessions/{sid}/rename", json={"title": "renamed"}, headers=auth)).json()["ok"]
        assert (await c.post(f"/api/teams/wb/sessions/{sid}/archive", json={}, headers=auth)).json()["ok"]


async def test_rest_session_writes_require_token(client):
    async with client as c:
        await c.post("/api/teams", json={"name": "wb"}, headers={"X-Admin-Token": settings.admin_token})
        assert (await c.post("/api/teams/wb/sessions", json={"agent": "bob"})).status_code == 401


async def test_session_busy_reflects_pending_directive(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    console = Caller(tid, "console:c", "c", "rest-console:c")
    sid = (await sessions_svc.create_session(tid, bob.agent_id, "t"))["session_id"]
    assert (await sessions_svc.get_session(tid, sid))["busy"] is False

    did = (await directives_svc.send_directive(console, "bob", "x", session_id=sid))["directive_id"]
    s = await sessions_svc.get_session(tid, sid)
    assert s["busy"] is True and s["last_status"] == "pending"                   # pending → busy
    assert (await sessions_svc.list_sessions(tid, bob.agent_id))[0]["last_status"] == "pending"

    await directives_svc.claim_directive(bob, did)
    assert (await sessions_svc.get_session(tid, sid))["last_status"] == "running"
    await directives_svc.report_directive(bob, did, "done", "ok")
    s = await sessions_svc.get_session(tid, sid)
    assert s["busy"] is False and s["last_status"] == "done"                     # terminal → idle/done


async def test_session_messages_store_roundtrip(team):
    tid = team["team_id"]
    bob = await join(team, "bob")
    sid = (await sessions_svc.create_session(tid, bob.agent_id, "t"))["session_id"]
    assert await sessions_svc.get_messages(tid, sid) == []          # new session: empty, not None
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert (await sessions_svc.set_messages(tid, sid, msgs))["ok"]
    assert await sessions_svc.get_messages(tid, sid) == msgs        # cross-host memory persists
    assert await sessions_svc.get_messages(tid, "nope") is None     # unknown session


async def test_rest_session_memory(client):
    async with client as c:
        team = (await c.post("/api/teams", json={"name": "wb"},
                             headers={"X-Admin-Token": settings.admin_token})).json()
        jt = team["join_token"]; auth = {"Authorization": f"Bearer {jt}"}
        await c.post("/api/teams/wb/heartbeat", json={"display_name": "bob"}, headers=auth)
        sid = (await c.post("/api/teams/wb/sessions", json={"agent": "bob"}, headers=auth)).json()["session_id"]
        msgs = [{"role": "user", "content": "remember me"}]
        assert (await c.post(f"/api/teams/wb/sessions/{sid}/memory", json={"messages": msgs}, headers=auth)).json()["ok"]
        assert (await c.get(f"/api/teams/wb/sessions/{sid}/memory", headers=auth)).json()["messages"] == msgs
        assert (await c.get(f"/api/teams/wb/sessions/{sid}/memory")).status_code == 401   # needs join token


async def test_workbench_page_served(client):
    async with client as c:
        r = await c.get("/workbench")
        assert r.status_code == 200 and "Workbench" in r.text


async def test_directive_failed_event_carries_session_id(team):
    # the workbench filters failed-events by session_id, so the payload must include it
    tid = team["team_id"]
    bob = await join(team, "bob")
    console = Caller(tid, "console:c", "c", "rest-console:c")
    sid = (await sessions_svc.create_session(tid, bob.agent_id, "t"))["session_id"]
    did = (await directives_svc.send_directive(console, "bob", "x", session_id=sid))["directive_id"]
    await directives_svc.claim_directive(bob, did)
    await directives_svc.report_directive(bob, did, "failed", "boom")
    from atmcp.services import status as status_svc
    ev = next(e for e in await status_svc.recent_events(tid, 50) if e["kind"] == "directive_failed")
    assert ev["payload"]["session_id"] == sid
