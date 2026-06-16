"""Append to the monotonic events log inside a transaction.

The returned event_id is the global per-team cursor used for catch-up/sync and to
drive the dashboard. The envelope is queued on the txn and published after commit.
"""

from __future__ import annotations

import json
from typing import Any

from atmcp.db import Tx
from atmcp.ids import now_ms

# Event kinds (also the dashboard activity-feed vocabulary).
AGENT_JOINED = "agent_joined"
AGENT_LEFT = "agent_left"
KNOWLEDGE_ADDED = "knowledge_added"
KNOWLEDGE_RETRACTED = "knowledge_retracted"
MEMORY_SET = "memory_set"
GOAL_CREATED = "goal_created"
TASK_CREATED = "task_created"
TASK_CLAIMED = "task_claimed"
TASK_PROGRESS = "task_progress"
TASK_COMPLETED = "task_completed"
TASK_FAILED = "task_failed"
TASK_REQUEUED = "task_requeued"
TASK_RELEASED = "task_released"
DIRECTIVE_SENT = "directive_sent"
DIRECTIVE_CLAIMED = "directive_claimed"
DIRECTIVE_DONE = "directive_done"
DIRECTIVE_FAILED = "directive_failed"
DIRECTIVE_CANCELED = "directive_canceled"
USAGE_REPORTED = "usage_reported"


async def append(
    tx: Tx,
    team_id: str,
    kind: str,
    entity_type: str,
    entity_id: str | None,
    actor_agent: str | None,
    payload: dict[str, Any] | None = None,
) -> int:
    payload = payload or {}
    ts = now_ms()
    cur = await tx.execute(
        "INSERT INTO events(team_id,kind,entity_type,entity_id,actor_agent,payload_json,ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (team_id, kind, entity_type, entity_id, actor_agent, json.dumps(payload), ts),
    )
    event_id = int(cur.lastrowid)
    tx.add_event(
        {
            "event_id": event_id,
            "team_id": team_id,
            "kind": kind,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor_agent": actor_agent,
            "payload": payload,
            "ts": ts,
        }
    )
    return event_id
