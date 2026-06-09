"""Caller identity resolution.

After `join_team` succeeds on a streamable-HTTP MCP session, we bind that session's
`Mcp-Session-Id` → (team, agent). Every later tool call carries the same header, so
we resolve identity with zero identity arguments in the tool surface. The in-process
map is primary; a Redis mirror lets us rehydrate after a transient hiccup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atmcp import db, redis_bus
from atmcp.ids import hash_token


class NotJoinedError(Exception):
    """Raised when a scoped tool is called before join_team on this session."""


@dataclass
class Caller:
    team_id: str
    agent_id: str
    display_name: str
    session_id: str


_sessions: dict[str, Caller] = {}


def bind(sid: str, team_id: str, agent_id: str, display_name: str) -> None:
    _sessions[sid] = Caller(team_id, agent_id, display_name, sid)


def unbind(sid: str) -> None:
    _sessions.pop(sid, None)


def session_id_from_ctx(ctx: Any) -> str | None:
    req = getattr(ctx.request_context, "request", None)
    if req is None:
        return None
    return req.headers.get("mcp-session-id")


def header_token_from_ctx(ctx: Any) -> str | None:
    """Team join token presented via the MCP client's configured headers."""
    req = getattr(ctx.request_context, "request", None)
    if req is None:
        return None
    auth = req.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return req.headers.get("x-atmcp-token")


def agent_name_from_ctx(ctx: Any) -> str | None:
    """Optional stable display_name supplied via the MCP client's headers."""
    req = getattr(ctx.request_context, "request", None)
    if req is None:
        return None
    return req.headers.get("x-atmcp-agent")


async def resolve(ctx: Any) -> Caller:
    sid = session_id_from_ctx(ctx)
    if not sid:
        raise NotJoinedError("no MCP session id on request; call join_team first")
    caller = _sessions.get(sid)
    if caller is not None:
        return caller
    # Rehydrate from the Redis mirror + DB (e.g. after a transient disconnect).
    rec = await redis_bus.get_session(sid)
    if rec:
        row = await db.fetchone(
            "SELECT display_name FROM agents WHERE team_id=? AND agent_id=?",
            (rec["team_id"], rec["agent_id"]),
        )
        if row is not None:
            caller = Caller(rec["team_id"], rec["agent_id"], row["display_name"], sid)
            _sessions[sid] = caller
            return caller

    # Auto-join: if the request carries a valid join token, join transparently so the
    # first tool call works without an explicit join_team. The token maps to exactly one
    # team. Set the X-ATMcp-Agent header for a stable display_name across reconnects;
    # otherwise a per-session name is generated (which would create a fresh roster entry).
    token = header_token_from_ctx(ctx)
    if token:
        team = await db.fetchone(
            "SELECT name FROM teams WHERE join_token_hash=?", (hash_token(token),)
        )
        if team is not None:
            from atmcp.services import identity as identity_svc  # lazy: avoid import cycle

            display_name = agent_name_from_ctx(ctx) or f"agent-{sid[:8]}"
            try:
                await identity_svc.join_team(sid, team["name"], token, display_name)
            except Exception:  # noqa: BLE001  (e.g. a display_name race) — fall through
                pass
            caller = _sessions.get(sid)
            if caller is not None:
                return caller

    raise NotJoinedError("session not joined to a team; call join_team first")
