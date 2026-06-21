"""Agent output stream: lets the console 'view another agent's output'.

Workers append chunks (either via the append_output MCP tool, or a client hook posting
to the REST endpoint). Readers tail by monotonic `seq` with long-poll. Each append bumps
the hub generation (waking tailers) and pushes a live frame to the dashboard WebSocket.
Old chunks are pruned by the reaper.
"""

from __future__ import annotations

from typing import Any

from atmcp import db, hub
from atmcp.config import settings
from atmcp.ids import now_ms


async def append_output(
    team_id: str,
    agent_id: str,
    text: str,
    directive_id: str | None = None,
    source: str = "agent",
    session_id: str | None = None,
) -> dict[str, Any]:
    text = (text or "")[: settings.output_max_chunk]
    now = now_ms()
    async with db.transaction() as tx:
        # Only keep the directive tag if it refers to a real directive in THIS team;
        # otherwise drop the tag (don't lose the output chunk over a stale/foreign id).
        if directive_id is not None:
            ref = await tx.fetchval(
                "SELECT 1 FROM directives WHERE team_id=? AND directive_id=?",
                (team_id, directive_id),
            )
            if not ref:
                directive_id = None
        # Same for the session tag: keep only a real session in this team.
        if session_id is not None:
            ref = await tx.fetchval(
                "SELECT 1 FROM sessions WHERE team_id=? AND session_id=?",
                (team_id, session_id),
            )
            if not ref:
                session_id = None
        cur = await tx.execute(
            "INSERT INTO agent_output(team_id,agent_id,session_id,directive_id,source,text,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (team_id, agent_id, session_id, directive_id, source, text, now),
        )
        seq = int(cur.lastrowid)
    await hub.tick(
        team_id,
        {"type": "output", "agent_id": agent_id, "session_id": session_id,
         "directive_id": directive_id, "seq": seq, "text": text, "ts": now},
    )
    return {"ok": True, "seq": seq}


async def get_output(
    team_id: str,
    agent_id: str,
    since_seq: int = 0,
    wait_ms: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    async def read():
        rows = await db.fetchall(
            "SELECT id,directive_id,source,text,ts FROM agent_output "
            "WHERE team_id=? AND agent_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (team_id, agent_id, int(since_seq or 0), max(1, min(int(limit or 200), 500))),
        )
        return [
            {"seq": r["id"], "directive_id": r["directive_id"], "source": r["source"],
             "text": r["text"], "ts": r["ts"]}
            for r in rows
        ]

    gen0 = hub.current_gen(team_id)
    chunks = await read()
    if not chunks and wait_ms and wait_ms > 0:
        if await hub.wait_for_change(team_id, gen0, min(int(wait_ms), 30000) / 1000.0):
            chunks = await read()
    head = int(await db.fetchval(
        "SELECT COALESCE(MAX(id),0) FROM agent_output WHERE team_id=? AND agent_id=?",
        (team_id, agent_id),
    ) or 0)
    return {"ok": True, "count": len(chunks), "chunks": chunks, "head_seq": head}


async def prune_expired(retention_ms: int) -> int:
    cutoff = now_ms() - retention_ms
    async with db.transaction() as tx:
        cur = await tx.execute("DELETE FROM agent_output WHERE ts < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
