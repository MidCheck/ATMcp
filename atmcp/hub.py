"""In-process fan-out hub (single-worker MVP).

Responsibilities:
  * Track connected dashboard WebSockets per team and broadcast frames to them.
  * Wake up long-polling `sync` callers when new events land for their team.

Wake-ups use a per-team monotonic generation counter guarded by an asyncio.Condition
(NOT a set/clear Event). A long-poll caller reads `current_gen()` BEFORE its DB read,
then waits for the generation to advance past that value — so an event dispatched in the
window between the read and the wait cannot be missed (no lost wakeups).

In-process broadcast keeps the live dashboard working even if Redis is down. Redis still
receives XADD/PUBLISH (see redis_bus.publish_events) so a future multi-worker deployment
can switch the WS source to a Redis pattern subscription. Frames carry event_id, so
duplicate/out-of-order delivery is idempotent on the client.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("atmcp.hub")

# team_id -> set of WebSocket-like objects exposing async send_json
_ws_clients: dict[str, set[Any]] = {}
# team_id -> Condition + monotonic generation counter (bumped per dispatched batch)
_conds: dict[str, asyncio.Condition] = {}
_gen: dict[str, int] = {}


def _cond(team: str) -> asyncio.Condition:
    c = _conds.get(team)
    if c is None:
        c = asyncio.Condition()
        _conds[team] = c
        _gen.setdefault(team, 0)
    return c


def current_gen(team: str) -> int:
    return _gen.get(team, 0)


def register_ws(team: str, ws: Any) -> None:
    _ws_clients.setdefault(team, set()).add(ws)


def unregister_ws(team: str, ws: Any) -> None:
    conns = _ws_clients.get(team)
    if conns:
        conns.discard(ws)
        if not conns:
            _ws_clients.pop(team, None)


def client_count(team: str) -> int:
    return len(_ws_clients.get(team, ()))


async def _broadcast(team: str, frame: dict[str, Any]) -> None:
    conns = list(_ws_clients.get(team, ()))
    if not conns:
        return
    dead = []
    for ws in conns:
        try:
            await ws.send_json(frame)
        except Exception:  # noqa: BLE001  (client gone/slow)
            dead.append(ws)
    for ws in dead:
        unregister_ws(team, ws)


async def dispatch(events: list[dict[str, Any]]) -> None:
    """Publisher hook (db.set_publisher). Broadcast committed events + wake waiters."""
    touched: set[str] = set()
    for ev in events:
        team = ev["team_id"]
        touched.add(team)
        await _broadcast(team, {"type": "event", **ev})
    for team in touched:
        c = _cond(team)
        async with c:
            _gen[team] = _gen.get(team, 0) + 1
            c.notify_all()


async def publish_presence(team: str, frame: dict[str, Any]) -> None:
    """Transient presence frame (not persisted, does not advance the sync generation)."""
    await _broadcast(team, {"type": "presence", **frame})


async def tick(team: str, frame: dict[str, Any] | None = None) -> None:
    """Advance the team generation (wake long-poll waiters) without writing an event row.
    Used by high-frequency streams like agent output. Optionally broadcasts a WS frame."""
    if frame is not None:
        await _broadcast(team, frame)
    c = _cond(team)
    async with c:
        _gen[team] = _gen.get(team, 0) + 1
        c.notify_all()


async def wait_for_change(team: str, since_gen: int, timeout_s: float) -> bool:
    """Block until the team's generation advances past since_gen (or timeout)."""
    c = _cond(team)
    async with c:
        if _gen.get(team, 0) > since_gen:
            return True
        try:
            await asyncio.wait_for(
                c.wait_for(lambda: _gen.get(team, 0) > since_gen), timeout=timeout_s
            )
            return True
        except asyncio.TimeoutError:
            return False
