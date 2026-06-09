"""Per-team Lamport logical clock. Centrally bumped on each memory write so memory
updates are totally ordered per team (last-writer-wins is deterministic)."""

from __future__ import annotations

from atmcp.db import Tx


async def bump(tx: Tx, team_id: str) -> int:
    row = await tx.fetchone(
        "INSERT INTO team_clock(team_id, lclock) VALUES(?, 1) "
        "ON CONFLICT(team_id) DO UPDATE SET lclock = lclock + 1 RETURNING lclock",
        (team_id,),
    )
    return int(row[0])
