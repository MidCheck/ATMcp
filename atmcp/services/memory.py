"""Shared memory: a per-key LWW-register ordered by the team Lamport clock.

The central clock linearizes writes, so the newest write always wins deterministically
(writer_agent is stored as the tiebreak for a future multi-master setup). Overwritten
values are appended to `memory_history` so lost writes are inspectable, and an optional
`expected_version` gives optimistic CAS that surfaces conflicts as data.
"""

from __future__ import annotations

import json
from typing import Any

from atmcp import db, events, idempotency
from atmcp.canonical import canonical_json
from atmcp.ids import now_ms
from atmcp.services import clock
from atmcp.session import Caller


async def set_memory(
    caller: Caller,
    key: str,
    value: Any,
    expected_version: int | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    value_json = canonical_json(value)
    now = now_ms()

    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, idem_key)
            if prior is not None:
                return prior
        cur = await tx.fetchone(
            "SELECT value_json, lclock, writer_agent, version FROM memory_current "
            "WHERE team_id=? AND mem_key=?",
            (team_id, key),
        )
        cur_version = cur["version"] if cur is not None else 0
        if expected_version is not None and int(expected_version) != cur_version:
            conflict = {
                "conflict": True,
                "key": key,
                "current_value": json.loads(cur["value_json"]) if cur is not None else None,
                "current_version": cur_version,
                "current_writer": cur["writer_agent"] if cur is not None else None,
            }
            if idem_key:
                await idempotency.store(tx, team_id, idem_key, conflict)
            return conflict

        lclock = await clock.bump(tx, team_id)
        new_version = cur_version + 1
        eid = await events.append(
            tx, team_id, events.MEMORY_SET, "memory", key, agent_id,
            {"version": new_version, "lclock": lclock},
        )
        if cur is None:
            await tx.execute(
                "INSERT INTO memory_current(team_id,mem_key,value_json,lclock,writer_agent,"
                "updated_at,version,last_event_id) VALUES(?,?,?,?,?,?,?,?)",
                (team_id, key, value_json, lclock, agent_id, now, new_version, eid),
            )
        else:
            await tx.execute(
                "UPDATE memory_current SET value_json=?, lclock=?, writer_agent=?, updated_at=?, "
                "version=?, last_event_id=? WHERE team_id=? AND mem_key=?",
                (value_json, lclock, agent_id, now, new_version, eid, team_id, key),
            )
        await tx.execute(
            "INSERT INTO memory_history(team_id,mem_key,value_json,lclock,writer_agent,version,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (team_id, key, value_json, lclock, agent_id, new_version, now),
        )
        result = {"ok": True, "key": key, "version": new_version, "lclock": lclock}
        if idem_key:
            await idempotency.store(tx, team_id, idem_key, result)
    return result


async def get_memory(caller: Caller, key: str | None = None) -> dict[str, Any]:
    team_id = caller.team_id
    if key is not None:
        row = await db.fetchone(
            "SELECT value_json, lclock, writer_agent, version, updated_at FROM memory_current "
            "WHERE team_id=? AND mem_key=?",
            (team_id, key),
        )
        if row is None:
            return {"found": False, "key": key}
        return {
            "found": True,
            "key": key,
            "value": json.loads(row["value_json"]),
            "version": row["version"],
            "lclock": row["lclock"],
            "writer": row["writer_agent"],
            "updated_at": row["updated_at"],
        }

    rows = await db.fetchall(
        "SELECT mem_key, value_json, version, writer_agent, updated_at FROM memory_current "
        "WHERE team_id=? ORDER BY mem_key",
        (team_id,),
    )
    return {
        "keys": {
            r["mem_key"]: {
                "value": json.loads(r["value_json"]),
                "version": r["version"],
                "writer": r["writer_agent"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        }
    }
