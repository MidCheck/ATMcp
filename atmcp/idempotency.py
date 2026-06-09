"""Durable idempotency, checked and stored INSIDE the write transaction.

Because the check + the work + the store all run under the single-writer lock, two
concurrent calls with the same (team, idem_key) can never both apply: the first
commits its result, the second sees it on check() and returns the stored result.
Unlike a Redis TTL cache this is race-free and survives a Redis outage. Rows are
pruned by the reaper (see prune_expired)."""

from __future__ import annotations

import json
from typing import Any

from atmcp import db
from atmcp.db import Tx
from atmcp.ids import now_ms


async def check(tx: Tx, team_id: str, idem_key: str) -> dict[str, Any] | None:
    row = await tx.fetchone(
        "SELECT result_json FROM idempotency WHERE team_id=? AND idem_key=?",
        (team_id, idem_key),
    )
    return json.loads(row["result_json"]) if row is not None else None


async def store(tx: Tx, team_id: str, idem_key: str, result: dict[str, Any]) -> None:
    # OR IGNORE: first writer wins; a racing duplicate keeps the original result.
    await tx.execute(
        "INSERT OR IGNORE INTO idempotency(team_id, idem_key, result_json, created_at) "
        "VALUES(?,?,?,?)",
        (team_id, idem_key, json.dumps(result), now_ms()),
    )


async def prune_expired(retention_ms: int) -> int:
    cutoff = now_ms() - retention_ms
    async with db.transaction() as tx:
        cur = await tx.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
