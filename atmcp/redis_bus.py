"""Redis = soft state only. Every call here is best-effort: if Redis is down we
degrade liveness (presence, leases, live fan-out) but never correctness — SQLite
remains the source of truth and rebuilds Redis on recovery.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from atmcp.config import settings

log = logging.getLogger("atmcp.redis")

_PREFIX = "atmcp"
_LUA_EXTEND = (Path(__file__).parent / "lua" / "extend_lease.lua").read_text(encoding="utf-8")

_client: aioredis.Redis | None = None
_extend_script: Any = None


# ── key helpers ─────────────────────────────────────────────────────────────
def hb_key(team: str, agent: str) -> str:
    return f"{_PREFIX}:hb:{team}:{agent}"


def lease_key(team: str, task: str) -> str:
    return f"{_PREFIX}:lease:{team}:{task}"


def events_channel(team: str) -> str:
    return f"{_PREFIX}:team:{team}:events"


def events_stream(team: str) -> str:
    return f"{_PREFIX}:team:{team}:log"


def session_key(sid: str) -> str:
    return f"{_PREFIX}:session:{sid}"


# ── lifecycle ───────────────────────────────────────────────────────────────
async def init() -> None:
    global _client, _extend_script
    _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    _extend_script = _client.register_script(_LUA_EXTEND)
    try:
        await _client.ping()
        log.info("redis connected: %s", settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("redis not reachable at startup (%s); running degraded", exc)


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
        _client = None


async def ping() -> bool:
    if _client is None:
        return False
    try:
        return bool(await _client.ping())
    except Exception:  # noqa: BLE001
        return False


# ── presence (heartbeats) ───────────────────────────────────────────────────
async def set_heartbeat(team: str, agent: str, payload: dict[str, Any], ttl_s: int) -> None:
    if _client is None:
        return
    try:
        await _client.set(hb_key(team, agent), json.dumps(payload), ex=ttl_s)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_heartbeat failed: %s", exc)


async def del_heartbeat(team: str, agent: str) -> None:
    if _client is None:
        return
    try:
        await _client.delete(hb_key(team, agent))
    except Exception:  # noqa: BLE001
        pass


async def get_heartbeats(team: str, agent_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """Pipeline GET for a set of agents → {agent_id: payload|None}. {} if Redis down."""
    if _client is None or not agent_ids:
        return {a: None for a in agent_ids}
    try:
        pipe = _client.pipeline()
        for a in agent_ids:
            pipe.get(hb_key(team, a))
        raw = await pipe.execute()
    except Exception:  # noqa: BLE001
        return {a: None for a in agent_ids}
    out: dict[str, dict[str, Any] | None] = {}
    for a, r in zip(agent_ids, raw):
        out[a] = json.loads(r) if r else None
    return out


# ── task leases (mirror of SQLite lease_expires_at; DB is authority) ─────────
async def set_lease(team: str, task: str, agent: str, fencing_token: int, ttl_s: int) -> None:
    if _client is None:
        return
    try:
        val = json.dumps({"agent_id": agent, "fencing_token": fencing_token})
        await _client.set(lease_key(team, task), val, px=ttl_s * 1000)
    except Exception:  # noqa: BLE001
        pass


async def extend_lease(team: str, task: str, agent: str, ttl_s: int) -> int:
    """Returns 1 extended / 0 absent / -1 other-holder / 0 if Redis down."""
    if _client is None or _extend_script is None:
        return 0
    try:
        res = await _extend_script(keys=[lease_key(team, task)], args=[agent, ttl_s * 1000])
        return int(res)
    except Exception:  # noqa: BLE001
        return 0


async def del_lease(team: str, task: str) -> None:
    if _client is None:
        return
    try:
        await _client.delete(lease_key(team, task))
    except Exception:  # noqa: BLE001
        pass


# ── session binding (durable mirror of the in-proc map) ─────────────────────
async def set_session(sid: str, team: str, agent: str, ttl_s: int) -> None:
    if _client is None:
        return
    try:
        await _client.set(session_key(sid), json.dumps({"team_id": team, "agent_id": agent}), ex=ttl_s)
    except Exception:  # noqa: BLE001
        pass


async def get_session(sid: str) -> dict[str, Any] | None:
    if _client is None:
        return None
    try:
        raw = await _client.get(session_key(sid))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def del_session(sid: str) -> None:
    if _client is None:
        return
    try:
        await _client.delete(session_key(sid))
    except Exception:  # noqa: BLE001
        pass


# ── event fan-out (Stream hot-window + pub/sub for multi-process future) ────
async def publish_events(events: list[dict[str, Any]]) -> None:
    if _client is None or not events:
        return
    try:
        pipe = _client.pipeline()
        for ev in events:
            team = ev["team_id"]
            data = json.dumps(ev)
            pipe.xadd(events_stream(team), {"data": data}, maxlen=settings.stream_maxlen, approximate=True)
            pipe.publish(events_channel(team), data)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        log.debug("publish_events failed: %s", exc)
