"""Sessions (threads): a conversation line with one agent — the workbench unit.

One session = one chat thread = one independent memory. A directive sent into a session
and the output it produces both carry the session_id, so the workbench can show a focused
transcript per thread. Sessions are durable in SQLite (so any device resumes them). The
executor's resumable session id / worktree are recorded back by the worker (1b).

A NULL session_id elsewhere (directives/output) is the agent's default/legacy thread — that
is what keeps the existing /team console and old pollers working unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from atmcp import db, events, hub
from atmcp.ids import new_id, now_ms


def _row(r) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


async def create_session(
    team_id: str, agent_id: str, title: str | None = None,
    driver: str | None = None, actor: str | None = None,
) -> dict[str, Any]:
    sid = new_id()
    now = now_ms()
    title = (title or "New session").strip()[:200] or "New session"
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO sessions(session_id,team_id,agent_id,title,driver,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'active',?,?)",
            (sid, team_id, agent_id, title, driver, now, now),
        )
        eid = await events.append(
            tx, team_id, events.SESSION_CREATED, "session", sid, actor,
            {"agent_id": agent_id, "title": title},
        )
        await tx.execute(
            "UPDATE sessions SET last_event_id=? WHERE team_id=? AND session_id=?",
            (eid, team_id, sid),
        )
    return {"ok": True, "session_id": sid, "team_id": team_id, "agent_id": agent_id,
            "title": title, "status": "active", "created_at": now, "updated_at": now}


async def get_session(team_id: str, session_id: str) -> dict[str, Any] | None:
    r = await db.fetchone(
        "SELECT session_id,team_id,agent_id,title,driver,cli_session_id,worktree,status,"
        "created_at,updated_at FROM sessions WHERE team_id=? AND session_id=?",
        (team_id, session_id),
    )
    return _row(r) if r is not None else None


async def list_sessions(
    team_id: str, agent_id: str | None = None, include_archived: bool = False, limit: int = 200
) -> list[dict[str, Any]]:
    clauses = ["team_id=?"]
    params: list[Any] = [team_id]
    if agent_id:
        clauses.append("agent_id=?")
        params.append(agent_id)
    if not include_archived:
        clauses.append("status='active'")
    params.append(max(1, min(int(limit or 200), 500)))
    rows = await db.fetchall(
        "SELECT session_id,agent_id,title,driver,status,created_at,updated_at "
        f"FROM sessions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
        params,
    )
    return [_row(r) for r in rows]


async def rename_session(team_id: str, session_id: str, title: str) -> dict[str, Any]:
    title = (title or "").strip()[:200]
    if not title:
        return {"ok": False, "error": "empty_title"}
    now = now_ms()
    async with db.transaction() as tx:
        cur = await tx.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE team_id=? AND session_id=?",
            (title, now, team_id, session_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "unknown_session"}
        await events.append(tx, team_id, events.SESSION_UPDATED, "session", session_id, None,
                            {"title": title})
    return {"ok": True, "session_id": session_id, "title": title}


async def archive_session(team_id: str, session_id: str) -> dict[str, Any]:
    now = now_ms()
    async with db.transaction() as tx:
        cur = await tx.execute(
            "UPDATE sessions SET status='archived', updated_at=? WHERE team_id=? AND session_id=?",
            (now, team_id, session_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "unknown_session"}
        await events.append(tx, team_id, events.SESSION_ARCHIVED, "session", session_id, None, {})
    return {"ok": True, "session_id": session_id, "status": "archived"}


async def set_executor_state(
    team_id: str, session_id: str, cli_session_id: str | None = None, worktree: str | None = None
) -> dict[str, Any]:
    """The worker records the executor's resumable session id / worktree for this thread,
    so a later directive in the same thread resumes the same model session."""
    now = now_ms()
    async with db.transaction() as tx:
        cur = await tx.execute(
            "UPDATE sessions SET cli_session_id=COALESCE(?,cli_session_id), "
            "worktree=COALESCE(?,worktree), updated_at=? WHERE team_id=? AND session_id=?",
            (cli_session_id, worktree, now, team_id, session_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "unknown_session"}
    return {"ok": True, "session_id": session_id}


async def session_output(
    team_id: str, session_id: str, since_seq: int = 0, wait_ms: int = 0, limit: int = 300
) -> dict[str, Any]:
    """Tail one thread's output chunks by monotonic seq, with long-poll — the right-pane stream."""
    async def read():
        rows = await db.fetchall(
            "SELECT id,directive_id,source,text,ts FROM agent_output "
            "WHERE team_id=? AND session_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (team_id, session_id, int(since_seq or 0), max(1, min(int(limit or 300), 500))),
        )
        return [{"seq": r["id"], "directive_id": r["directive_id"], "source": r["source"],
                 "text": r["text"], "ts": r["ts"]} for r in rows]

    gen0 = hub.current_gen(team_id)
    chunks = await read()
    if not chunks and wait_ms and wait_ms > 0:
        if await hub.wait_for_change(team_id, gen0, min(int(wait_ms), 30000) / 1000.0):
            chunks = await read()
    head = int(await db.fetchval(
        "SELECT COALESCE(MAX(id),0) FROM agent_output WHERE team_id=? AND session_id=?",
        (team_id, session_id),
    ) or 0)
    return {"ok": True, "count": len(chunks), "chunks": chunks, "head_seq": head}


async def transcript(team_id: str, session_id: str) -> dict[str, Any]:
    """Initial load for the chat pane: the thread's user messages (directives) + output."""
    directives = await db.fetchall(
        "SELECT directive_id,instruction,status,result_summary,created_at,updated_at "
        "FROM directives WHERE team_id=? AND session_id=? ORDER BY created_at ASC",
        (team_id, session_id),
    )
    out = await session_output(team_id, session_id, 0, 0, 500)
    return {"directives": [_row(d) for d in directives],
            "output": out["chunks"], "head_seq": out["head_seq"]}
