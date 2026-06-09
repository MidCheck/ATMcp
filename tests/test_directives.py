"""Directive bus + agent output stream: targeted command flow, inbox isolation,
ownership checks, cancel, output tailing, and inbox long-poll wake-up."""

from __future__ import annotations

import asyncio

from atmcp.services import directives as d
from atmcp.services import output as o
from conftest import join


async def test_directive_round_trip(team):
    console = await join(team, "console")
    worker = await join(team, "worker")

    sent = await d.send_directive(console, "worker", "do task X", priority=5)
    assert sent["ok"]
    did = sent["directive_id"]

    inb = await d.inbox(worker)
    assert inb["count"] == 1 and inb["directives"][0]["directive_id"] == did

    assert (await d.claim_directive(worker, did))["ok"]
    await o.append_output(team["team_id"], worker.agent_id, "working on X...", did)
    rep = await d.report_directive(worker, did, "done", "finished X", "full result text")
    assert rep["ok"]

    waited = await d.wait_directive(console, did)
    assert waited["final"] is True
    dd = waited["directive"]
    assert dd["status"] == "done"
    assert dd["result_summary"] == "finished X"
    assert dd["result_output"] == "full result text"


async def test_send_to_unknown_agent(team):
    console = await join(team, "console")
    r = await d.send_directive(console, "ghost", "hi")
    assert not r["ok"] and r["error"] == "unknown_agent"


async def test_inbox_is_per_agent(team):
    console = await join(team, "console")
    a = await join(team, "a")
    b = await join(team, "b")
    await d.send_directive(console, "a", "for a only")
    assert (await d.inbox(a))["count"] == 1
    assert (await d.inbox(b))["count"] == 0


async def test_only_target_can_claim(team):
    console = await join(team, "console")
    await join(team, "worker")
    other = await join(team, "other")
    did = (await d.send_directive(console, "worker", "x"))["directive_id"]
    bad = await d.claim_directive(other, did)
    assert not bad["ok"] and bad["error"] == "not_yours"


async def test_cancel_removes_from_inbox(team):
    console = await join(team, "console")
    worker = await join(team, "worker")
    did = (await d.send_directive(console, "worker", "x"))["directive_id"]
    assert (await d.cancel_directive(console, did))["ok"]
    assert (await d.inbox(worker))["count"] == 0
    # worker cannot report a canceled directive
    assert not (await d.report_directive(worker, did, "done"))["ok"]


async def test_only_issuer_can_cancel(team):
    console = await join(team, "console")
    worker = await join(team, "worker")
    did = (await d.send_directive(console, "worker", "x"))["directive_id"]
    bad = await d.cancel_directive(worker, did)
    assert not bad["ok"] and bad["error"] == "not_yours"


async def test_output_tail_incremental(team):
    worker = await join(team, "worker")
    await o.append_output(team["team_id"], worker.agent_id, "line A")
    await o.append_output(team["team_id"], worker.agent_id, "line B")
    out = await o.get_output(team["team_id"], worker.agent_id, since_seq=0)
    assert [c["text"] for c in out["chunks"]] == ["line A", "line B"]

    head = out["head_seq"]
    await o.append_output(team["team_id"], worker.agent_id, "line C")
    out2 = await o.get_output(team["team_id"], worker.agent_id, since_seq=head)
    assert [c["text"] for c in out2["chunks"]] == ["line C"]


async def test_inbox_longpoll_wakes_on_send(team):
    console = await join(team, "console")
    worker = await join(team, "worker")

    async def sender():
        await asyncio.sleep(0.05)
        await d.send_directive(console, "worker", "async command")

    task = asyncio.create_task(sender())
    inb = await d.inbox(worker, wait_ms=3000)  # long-poll
    await task
    assert inb["count"] == 1


async def test_append_output_drops_invalid_directive_tag(team):
    worker = await join(team, "worker")
    # A stale/foreign directive_id is dropped to NULL, but the output is still kept.
    r = await o.append_output(team["team_id"], worker.agent_id, "stray line", "no-such-directive")
    assert r["ok"]
    out = await o.get_output(team["team_id"], worker.agent_id)
    assert out["count"] == 1
    assert out["chunks"][0]["text"] == "stray line"
    assert out["chunks"][0]["directive_id"] is None


async def test_list_directives_roles(team):
    console = await join(team, "console")
    worker = await join(team, "worker")
    await d.send_directive(console, "worker", "one")
    await d.send_directive(console, "worker", "two")
    assert len(await d.list_directives(console, role="sent")) == 2
    assert len(await d.list_directives(worker, role="received")) == 2
    assert len(await d.list_directives(worker, role="sent")) == 0
