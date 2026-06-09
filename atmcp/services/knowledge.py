"""Shared knowledge: append-only + content-addressed (sha256) → automatic dedupe
and provenance, modeled as an OR-Set with a fast `knowledge_current` projection and
an FTS5 search index."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from atmcp import db, events, idempotency
from atmcp.canonical import content_id, normalize_tags
from atmcp.ids import now_ms
from atmcp.session import Caller

log = logging.getLogger("atmcp.knowledge")


async def post_knowledge(
    caller: Caller,
    title: str,
    body: str,
    tags: list[str] | None = None,
    task_id: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    cid = content_id(title, body, tags)
    norm_tags = normalize_tags(tags)
    tags_json = json.dumps(norm_tags)
    now = now_ms()

    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, caller.agent_id, idem_key)
            if prior is not None:
                return prior
        await tx.execute(
            "INSERT OR IGNORE INTO knowledge_objects(content_id,title,body,tags_json,byte_size) "
            "VALUES(?,?,?,?,?)",
            (cid, title, body, tags_json, len(body.encode("utf-8"))),
        )
        eid = await events.append(
            tx, team_id, events.KNOWLEDGE_ADDED, "knowledge", cid, agent_id,
            {"title": title, "tags": norm_tags, "task_id": task_id},
        )
        await tx.execute(
            "INSERT INTO knowledge_contributions(team_id,content_id,author_agent,task_id,created_at,event_id) "
            "VALUES(?,?,?,?,?,?)",
            (team_id, cid, agent_id, task_id, now, eid),
        )
        ccount = await tx.fetchval(
            "SELECT COUNT(DISTINCT author_agent) FROM knowledge_contributions "
            "WHERE team_id=? AND content_id=?",
            (team_id, cid),
        )
        existing = await tx.fetchone(
            "SELECT content_id FROM knowledge_current WHERE team_id=? AND content_id=?",
            (team_id, cid),
        )
        if existing is None:
            await tx.execute(
                "INSERT INTO knowledge_current(team_id,content_id,title,body,tags_json,first_author,"
                "contributor_count,present,first_seen_at,last_seen_at,last_event_id) "
                "VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                (team_id, cid, title, body, tags_json, agent_id, ccount, now, now, eid),
            )
            deduped = False
        else:
            await tx.execute(
                "UPDATE knowledge_current SET present=1, contributor_count=?, last_seen_at=?, "
                "last_event_id=? WHERE team_id=? AND content_id=?",
                (ccount, now, eid, team_id, cid),
            )
            deduped = True
        # FTS upsert (standalone table → plain DELETE + INSERT).
        await tx.execute("DELETE FROM knowledge_fts WHERE team_id=? AND content_id=?", (team_id, cid))
        await tx.execute(
            "INSERT INTO knowledge_fts(team_id,content_id,title,body,tags) VALUES(?,?,?,?,?)",
            (team_id, cid, title, body, " ".join(norm_tags)),
        )
        result = {
            "content_id": cid,
            "event_id": eid,
            "deduped": deduped,
            "contributor_count": int(ccount),
        }
        if idem_key:
            await idempotency.store(tx, team_id, caller.agent_id, idem_key, result)
    return result


def _row_to_knowledge(r: aiosqlite.Row) -> dict[str, Any]:
    return {
        "content_id": r["content_id"],
        "title": r["title"],
        "body": r["body"],
        "tags": json.loads(r["tags_json"]),
        "first_author": r["first_author"],
        "contributor_count": r["contributor_count"],
        "last_event_id": r["last_event_id"],
    }


async def search_knowledge(
    caller: Caller,
    query: str | None = None,
    tags: list[str] | None = None,
    since_event_id: int = 0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    team_id = caller.team_id
    limit = max(1, min(int(limit or 50), 200))
    since = int(since_event_id or 0)
    rows: list[aiosqlite.Row] = []

    if query and query.strip():
        try:
            rows = await db.fetchall(
                "SELECT kc.content_id,kc.title,kc.body,kc.tags_json,kc.first_author,"
                "kc.contributor_count,kc.last_event_id "
                "FROM knowledge_fts f "
                "JOIN knowledge_current kc ON kc.team_id=f.team_id AND kc.content_id=f.content_id "
                "WHERE f.team_id=? AND f MATCH ? AND kc.present=1 AND kc.last_event_id>? "
                "ORDER BY f.rank LIMIT ?",
                (team_id, query, since, limit),
            )
        except aiosqlite.OperationalError as exc:
            # Malformed FTS expression → fall back to a substring scan.
            log.debug("FTS query failed (%s); falling back to LIKE", exc)
            like = f"%{query.strip()}%"
            rows = await db.fetchall(
                "SELECT content_id,title,body,tags_json,first_author,contributor_count,last_event_id "
                "FROM knowledge_current WHERE team_id=? AND present=1 AND last_event_id>? "
                "AND (title LIKE ? OR body LIKE ?) ORDER BY last_seen_at DESC LIMIT ?",
                (team_id, since, like, like, limit),
            )
    else:
        rows = await db.fetchall(
            "SELECT content_id,title,body,tags_json,first_author,contributor_count,last_event_id "
            "FROM knowledge_current WHERE team_id=? AND present=1 AND last_event_id>? "
            "ORDER BY last_seen_at DESC LIMIT ?",
            (team_id, since, limit),
        )

    out = [_row_to_knowledge(r) for r in rows]
    if tags:
        wanted = set(normalize_tags(tags))
        out = [r for r in out if wanted.issubset(set(r["tags"]))]
    return out


async def retract_knowledge(
    caller: Caller, content_id_: str, idem_key: str | None = None
) -> dict[str, Any]:
    team_id, agent_id = caller.team_id, caller.agent_id
    now = now_ms()
    async with db.transaction() as tx:
        if idem_key:
            prior = await idempotency.check(tx, team_id, caller.agent_id, idem_key)
            if prior is not None:
                return prior
        existing = await tx.fetchone(
            "SELECT present FROM knowledge_current WHERE team_id=? AND content_id=?",
            (team_id, content_id_),
        )
        if existing is None:
            return {"ok": False, "error": "unknown_content_id"}
        eid = await events.append(
            tx, team_id, events.KNOWLEDGE_RETRACTED, "knowledge", content_id_, agent_id, {}
        )
        await tx.execute(
            "UPDATE knowledge_current SET present=0, last_seen_at=?, last_event_id=? "
            "WHERE team_id=? AND content_id=?",
            (now, eid, team_id, content_id_),
        )
        await tx.execute(
            "DELETE FROM knowledge_fts WHERE team_id=? AND content_id=?", (team_id, content_id_)
        )
        result = {"ok": True, "content_id": content_id_, "event_id": eid}
        if idem_key:
            await idempotency.store(tx, team_id, caller.agent_id, idem_key, result)
    return result
