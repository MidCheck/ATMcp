"""SQLite data layer — the durable source of truth.

Design rules enforced here:
  * Exactly one writer connection, serialized by a single asyncio.Lock, so every
    multi-statement mutation runs as an atomic `BEGIN IMMEDIATE … COMMIT`.
  * A separate read connection (WAL) lets dashboard/reads proceed without queueing
    behind the writer.
  * `transaction()` collects "events" produced during the txn and, *after commit*,
    hands them to a registered publisher (commit-then-publish). The publisher is
    injected to avoid an import cycle with the Redis / fan-out layers.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

import aiosqlite

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_wconn: aiosqlite.Connection | None = None
_rconn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

# Publisher invoked with the list of event envelopes after a txn commits.
_publisher: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None


def set_publisher(fn: Callable[[list[dict[str, Any]]], Awaitable[None]]) -> None:
    global _publisher
    _publisher = fn


async def _open(path: str) -> aiosqlite.Connection:
    # isolation_level=None → autocommit; we manage BEGIN IMMEDIATE/COMMIT/ROLLBACK
    # ourselves. It must be passed at connect time (the setter runs on the wrong
    # thread for aiosqlite's worker connection).
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def init(path: str | None = None) -> None:
    """Open connections and apply the schema (idempotent)."""
    global _wconn, _rconn
    from atmcp.config import settings

    db_path = path or settings.sqlite_path
    if db_path != ":memory:":
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    _wconn = await _open(db_path)
    await _wconn.execute("PRAGMA journal_mode=WAL")
    await _wconn.execute("PRAGMA synchronous=NORMAL")
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    await _wconn.executescript(schema)
    await _wconn.commit()
    await _migrate(_wconn)

    # Read connection shares the same DB file (WAL → concurrent reads).
    _rconn = await _open(db_path)


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Tiny forward migrations for pre-existing dev databases."""
    cur = await conn.execute("PRAGMA table_info(idempotency)")
    cols = [r[1] for r in await cur.fetchall()]
    await cur.close()
    if cols and "agent_id" not in cols:
        # Idempotency rows are short-lived (pruned by the reaper); recreate with the
        # agent-scoped primary key. Safe to drop.
        await conn.execute("DROP TABLE idempotency")
        await conn.execute(
            "CREATE TABLE idempotency (team_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
            "idem_key TEXT NOT NULL, result_json TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "PRIMARY KEY (team_id, agent_id, idem_key))"
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency(created_at)")
        await conn.commit()


async def close() -> None:
    global _wconn, _rconn
    if _wconn is not None:
        await _wconn.close()
        _wconn = None
    if _rconn is not None:
        await _rconn.close()
        _rconn = None


class Tx:
    """A live write transaction. Collects event envelopes for post-commit publish."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.events: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, list(seq))

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None else None

    def add_event(self, env: dict[str, Any]) -> None:
        self.events.append(env)


@asynccontextmanager
async def transaction():
    """Serialized write transaction. Publishes collected events after commit."""
    assert _wconn is not None, "db.init() not called"
    async with _write_lock:
        tx = Tx(_wconn)
        await _wconn.execute("BEGIN IMMEDIATE")
        try:
            yield tx
            await _wconn.execute("COMMIT")
        except BaseException:
            await _wconn.execute("ROLLBACK")
            raise
    # Lock released, durable. Now fan out (best-effort; never affects correctness).
    if tx.events and _publisher is not None:
        await _publisher(tx.events)


# ── read-only helpers (separate connection) ─────────────────────────────────
async def fetchall(sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
    assert _rconn is not None, "db.init() not called"
    cur = await _rconn.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return list(rows)


async def fetchone(sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
    assert _rconn is not None, "db.init() not called"
    cur = await _rconn.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetchval(sql: str, params: Sequence[Any] = ()) -> Any:
    row = await fetchone(sql, params)
    return row[0] if row is not None else None
