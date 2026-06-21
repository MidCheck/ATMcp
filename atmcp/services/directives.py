"""Directive bus: point-to-point commands from a console agent to a specific worker.

Flow: console `send_directive(to=worker, "...")` → worker `inbox(wait_ms)` picks it up →
`claim_directive` → does the work (streaming via output.append_output) → `report_directive`
(done/failed, summary, output). The console `wait_directive(id, wait_ms)` long-polls for the
result. Reuses the events log + the hub generation counter for catch-up and wake-ups.
"""

from __future__ import annotations

import json
from typing import Any

from atmcp import db, events, hub, idempotency
from atmcp.ids import new_id, now_ms
from atmcp.services import identity as identity_svc
from atmcp.session import Caller

_TERMINAL = {"done", "failed", "canceled"}


def _row(r) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


async def _get(team_id: str, directive_id: str) -> dict[str, Any] | None:
    r = await db.fetchone(
        "SELECT directive_id,from_agent,to_agent,instruction,payload_json,status,priority,"
        "result_summary,result_output,created_at,updated_at FROM directives "
        "WHERE team_id=? AND directive_id=?",
        (team_id, directive_id),
    )
    if r is None:
        return None
    d = _row(r)
    d["payload"] = json.loads(d.pop("payload_json"))
    return d


async def send_directive(
    caller: Caller,
    to_agent: str,
    instruction: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    idem_key: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    team_id = caller.team_id
    target = await identity_svc.resolve_agent_ref(team_id, to_agent)
    if target is None:
        return {"ok": False, "error": "unknown_agent", "to_agent": to_agent}

    did = new_id()
    now = now_ms()
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, caller.agent_id, idem_key)
            if prior is not None:
                return prior
        # A session_id ties this directive to a workbench thread; only honor one that
        # exists in this team AND belongs to the target agent (else drop to the default thread).
        if session_id is not None:
            ok_sess = await tx.fetchval(
                "SELECT 1 FROM sessions WHERE team_id=? AND session_id=? AND agent_id=?",
                (team_id, session_id, target),
            )
            if not ok_sess:
                session_id = None
        await tx.execute(
            "INSERT INTO directives(directive_id,team_id,from_agent,to_agent,session_id,instruction,"
            "payload_json,status,priority,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
            (did, team_id, caller.agent_id, target, session_id, instruction, json.dumps(payload or {}),
             int(priority), now, now),
        )
        if session_id is not None:
            await tx.execute(
                "UPDATE sessions SET updated_at=? WHERE team_id=? AND session_id=?",
                (now, team_id, session_id),
            )
        eid = await events.append(
            tx, team_id, events.DIRECTIVE_SENT, "directive", did, caller.agent_id,
            {"to_agent": target, "instruction": instruction[:200], "session_id": session_id},
        )
        await tx.execute(
            "UPDATE directives SET last_event_id=? WHERE team_id=? AND directive_id=?",
            (eid, team_id, did),
        )
        result = {"ok": True, "directive_id": did, "to_agent": target,
                  "session_id": session_id, "event_id": eid}
        if idem_key:
            await idempotency.store(tx, team_id, caller.agent_id, idem_key, result)
    return result


async def _read_inbox(team_id: str, agent_id: str, statuses: tuple[str, ...], limit: int):
    placeholders = ",".join("?" * len(statuses))
    rows = await db.fetchall(
        f"SELECT directive_id,from_agent,session_id,instruction,payload_json,status,priority,created_at "
        f"FROM directives WHERE team_id=? AND to_agent=? AND status IN ({placeholders}) "
        f"ORDER BY priority DESC, created_at ASC LIMIT ?",
        (team_id, agent_id, *statuses, max(1, min(int(limit or 20), 100))),
    )
    return [
        {"directive_id": r["directive_id"], "from_agent": r["from_agent"],
         "session_id": r["session_id"], "instruction": r["instruction"],
         "payload": json.loads(r["payload_json"]),
         "status": r["status"], "priority": r["priority"], "created_at": r["created_at"]}
        for r in rows
    ]


async def inbox(
    caller: Caller, wait_ms: int = 0, limit: int = 20, include_running: bool = False
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    statuses = ("pending", "running") if include_running else ("pending",)
    gen0 = hub.current_gen(team_id)
    items = await _read_inbox(team_id, agent_id, statuses, limit)
    if not items and wait_ms and wait_ms > 0:
        if await hub.wait_for_change(team_id, gen0, min(int(wait_ms), 30000) / 1000.0):
            items = await _read_inbox(team_id, agent_id, statuses, limit)
    return {"ok": True, "count": len(items), "directives": items}


async def claim_directive(caller: Caller, directive_id: str) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        d = await tx.fetchone(
            "SELECT to_agent, status FROM directives WHERE team_id=? AND directive_id=?",
            (team_id, directive_id),
        )
        if d is None:
            return {"ok": False, "error": "unknown_directive"}
        if d["to_agent"] != agent_id:
            return {"ok": False, "error": "not_yours"}
        if d["status"] != "pending":
            return {"ok": False, "error": "not_pending", "status": d["status"]}
        cur = await tx.execute(
            "UPDATE directives SET status='running', updated_at=? "
            "WHERE team_id=? AND directive_id=? AND status='pending'",
            (now, team_id, directive_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "race_lost"}
        eid = await events.append(
            tx, team_id, events.DIRECTIVE_CLAIMED, "directive", directive_id, agent_id, {}
        )
        await tx.execute(
            "UPDATE directives SET last_event_id=? WHERE team_id=? AND directive_id=?",
            (eid, team_id, directive_id),
        )
    return {"ok": True, "directive_id": directive_id}


async def report_directive(
    caller: Caller,
    directive_id: str,
    status: str,
    result_summary: str | None = None,
    output: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    if status not in ("done", "failed"):
        return {"ok": False, "error": "bad_status", "hint": "use 'done' or 'failed'"}
    now = now_ms()
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, caller.agent_id, idem_key)
            if prior is not None:
                return prior
        d = await tx.fetchone(
            "SELECT to_agent, status FROM directives WHERE team_id=? AND directive_id=?",
            (team_id, directive_id),
        )
        if d is None:
            result: dict[str, Any] = {"ok": False, "error": "unknown_directive"}
        elif d["to_agent"] != agent_id:
            result = {"ok": False, "error": "not_yours"}
        elif d["status"] in _TERMINAL:
            result = {"ok": False, "error": "already_final", "status": d["status"]}
        else:
            await tx.execute(
                "UPDATE directives SET status=?, result_summary=?, result_output=?, updated_at=? "
                "WHERE team_id=? AND directive_id=?",
                (status, result_summary, output, now, team_id, directive_id),
            )
            kind = events.DIRECTIVE_DONE if status == "done" else events.DIRECTIVE_FAILED
            eid = await events.append(
                tx, team_id, kind, "directive", directive_id, agent_id,
                {"result_summary": result_summary},
            )
            await tx.execute(
                "UPDATE directives SET last_event_id=? WHERE team_id=? AND directive_id=?",
                (eid, team_id, directive_id),
            )
            result = {"ok": True, "directive_id": directive_id, "status": status}
        if idem_key:
            await idempotency.store(tx, team_id, caller.agent_id, idem_key, result)
    return result


async def wait_directive(caller: Caller, directive_id: str, wait_ms: int = 0) -> dict[str, Any]:
    """Long-poll for a directive to reach a terminal state (the console's 'notify')."""
    team_id = caller.team_id
    gen0 = hub.current_gen(team_id)
    d = await _get(team_id, directive_id)
    if d is None:
        return {"ok": False, "error": "unknown_directive"}
    if d["status"] in _TERMINAL or not wait_ms or wait_ms <= 0:
        return {"ok": True, "final": d["status"] in _TERMINAL, "directive": d}
    if await hub.wait_for_change(team_id, gen0, min(int(wait_ms), 30000) / 1000.0):
        d = await _get(team_id, directive_id)
    return {"ok": True, "final": d["status"] in _TERMINAL, "directive": d}


async def cancel_directive(caller: Caller, directive_id: str) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        d = await tx.fetchone(
            "SELECT from_agent, status FROM directives WHERE team_id=? AND directive_id=?",
            (team_id, directive_id),
        )
        if d is None:
            return {"ok": False, "error": "unknown_directive"}
        if d["from_agent"] != agent_id:
            return {"ok": False, "error": "not_yours"}
        if d["status"] in _TERMINAL:
            return {"ok": False, "error": "already_final", "status": d["status"]}
        await tx.execute(
            "UPDATE directives SET status='canceled', updated_at=? "
            "WHERE team_id=? AND directive_id=?",
            (now, team_id, directive_id),
        )
        eid = await events.append(
            tx, team_id, events.DIRECTIVE_CANCELED, "directive", directive_id, agent_id, {}
        )
        await tx.execute(
            "UPDATE directives SET last_event_id=? WHERE team_id=? AND directive_id=?",
            (eid, team_id, directive_id),
        )
    return {"ok": True, "directive_id": directive_id}


async def list_team(
    team_id: str, status: str | None = None, to_agent: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Team-wide directive list (for the dashboard, not scoped to a caller's role)."""
    clauses = ["team_id=?"]
    params: list[Any] = [team_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    if to_agent:
        clauses.append("to_agent=?")
        params.append(to_agent)
    params.append(max(1, min(int(limit or 100), 300)))
    rows = await db.fetchall(
        "SELECT directive_id,from_agent,to_agent,instruction,status,priority,result_summary,"
        f"created_at,updated_at FROM directives WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        params,
    )
    return [_row(r) for r in rows]


async def list_directives(
    caller: Caller, role: str | None = None, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    team_id, agent_id = caller.team_id, caller.agent_id
    clauses = ["team_id=?"]
    params: list[Any] = [team_id]
    if role == "received":
        clauses.append("to_agent=?")
        params.append(agent_id)
    elif role == "sent":
        clauses.append("from_agent=?")
        params.append(agent_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    params.append(max(1, min(int(limit or 50), 200)))
    rows = await db.fetchall(
        "SELECT directive_id,from_agent,to_agent,instruction,status,priority,result_summary,"
        f"created_at,updated_at FROM directives WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT ?",
        params,
    )
    return [_row(r) for r in rows]
