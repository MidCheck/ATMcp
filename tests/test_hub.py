"""Hub long-poll wake-up: no lost wakeups (generation counter), wake on dispatch, timeout."""

from __future__ import annotations

import asyncio

import pytest

from atmcp import hub


def _ev(team):
    return [{
        "team_id": team, "event_id": 1, "kind": "x", "entity_type": "y",
        "entity_id": "z", "actor_agent": None, "payload": {}, "ts": 0,
    }]


@pytest.fixture(autouse=True)
def _clean_hub():
    yield
    hub._conds.clear()
    hub._gen.clear()
    hub._ws_clients.clear()


async def test_no_lost_wakeup_when_event_precedes_wait():
    team = "t1"
    gen0 = hub.current_gen(team)
    # Event dispatched in the window BEFORE we wait — generation already advanced.
    await hub.dispatch(_ev(team))
    got = await hub.wait_for_change(team, gen0, timeout_s=0.5)
    assert got is True  # must not block until timeout


async def test_wakes_on_later_dispatch():
    team = "t2"
    gen0 = hub.current_gen(team)

    async def later():
        await asyncio.sleep(0.05)
        await hub.dispatch(_ev(team))

    task = asyncio.create_task(later())
    got = await hub.wait_for_change(team, gen0, timeout_s=2)
    await task
    assert got is True


async def test_timeout_returns_false():
    team = "t3"
    got = await hub.wait_for_change(team, hub.current_gen(team), timeout_s=0.1)
    assert got is False
