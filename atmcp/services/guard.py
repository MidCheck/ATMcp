"""Command Guard: a safety gate for tool calls (especially shell) made by agents.

Critical once LOCAL models run Bash — the highest-risk surface. The decision pipeline is
deterministic and fast: team deny rules → a built-in dangerous-command deny-list → team allow
rules → default allow. Every check is written to an append-only audit (`guard_events`) and a
deny also emits a GUARD_BLOCKED event for the dashboard. This is the static layer of the
layered design; an LLM-reviewer / human-ask escalation tier can be added later.

`match_builtin()` is a pure function so the worker host can ship the same deny-list as an
offline fail-closed fallback when the server guard is unreachable.
"""

from __future__ import annotations

import re
from typing import Any

from atmcp import db, events
from atmcp.ids import now_ms

# (label, compiled regex, reason). Case-insensitive. Targeted at catastrophic / exfil actions,
# not everyday commands — teams add their own rules for the rest.
_BUILTIN: list[tuple[str, re.Pattern, str]] = [
    ("rm_rf_root", re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:-[a-z]+\s+)*(?:/|~|\$HOME)(?:\s|/|\*|$)", re.I),
     "recursive force-remove of / or home"),
    ("rm_rf_wildcard", re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:-[a-z]+\s+)*[/~]?\*", re.I),
     "recursive force-remove of a wildcard"),
    ("fork_bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:"), "fork bomb"),
    ("mkfs", re.compile(r"\bmkfs(\.\w+)?\b", re.I), "filesystem format"),
    ("dd_device", re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.I), "dd to a device"),
    ("write_block_device", re.compile(r">\s*/dev/(sd|nvme|disk|hd|mmcblk)", re.I), "write to a block device"),
    ("pipe_to_shell", re.compile(r"\b(curl|wget|fetch)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|fish)\b", re.I),
     "piping a download straight into a shell"),
    ("chmod_777_root", re.compile(r"\bchmod\s+-R\s+777\s+/", re.I), "world-writable recursively from /"),
    ("shutdown", re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "power/shutdown command"),
    ("kill_all", re.compile(r"\bkill\s+-9\s+-1\b|\bkillall\s+-9\b", re.I), "kill everything"),
    ("sudo", re.compile(r"(^|\s|;|&|\|)sudo\s", re.I), "privilege escalation (sudo)"),
    ("secret_exfil", re.compile(r"\b(cat|less|more|cp|scp|curl|tar|cat)\b[^\n]*"
                               r"(\.ssh/|\.aws/credentials|\.env(\s|$)|id_rsa|id_ed25519|authorized_keys)", re.I),
     "reading/copying secrets or keys"),
    ("authorized_keys_write", re.compile(r">{1,2}\s*[^\n]*authorized_keys", re.I), "writing SSH authorized_keys"),
    ("git_force_push", re.compile(r"\bgit\s+push\b[^\n]*(--force(?!-with-lease)|\s-f(\s|$))", re.I),
     "git force-push"),
]


def match_builtin(command: str) -> tuple[str, str] | None:
    """Pure deny-list check. Returns (label, reason) if the command is dangerous, else None.
    Shared verbatim by the worker host as an offline fail-closed fallback."""
    cmd = command or ""
    for label, rx, reason in _BUILTIN:
        if rx.search(cmd):
            return (label, reason)
    return None


def _rule_matches(pattern: str, ptype: str, cmd: str) -> bool:
    if ptype == "regex":
        try:
            return re.search(pattern, cmd, re.I) is not None
        except re.error:
            return False
    return pattern.lower() in cmd.lower()


async def check(
    team_id: str, agent_id: str | None, command: str,
    tool: str | None = None, session_id: str | None = None,
) -> dict[str, Any]:
    """Decide allow/deny for a command; audit it; emit GUARD_BLOCKED on deny."""
    cmd = command or ""
    decision, reason, rule = "allow", None, None

    rules = await db.fetchall(
        "SELECT id,kind,pattern,pattern_type FROM guard_rules WHERE team_id=? AND enabled=1 ORDER BY id",
        (team_id,),
    )
    for r in rules:  # team deny wins first
        if r["kind"] == "deny" and _rule_matches(r["pattern"], r["pattern_type"], cmd):
            decision, reason, rule = "deny", "team deny rule", f"team:{r['id']}"
            break
    if decision == "allow":
        b = match_builtin(cmd)
        if b:
            decision, reason, rule = "deny", b[1], f"builtin:{b[0]}"
    if decision == "allow":
        for r in rules:
            if r["kind"] == "allow" and _rule_matches(r["pattern"], r["pattern_type"], cmd):
                reason, rule = "team allow rule", f"team:{r['id']}"
                break

    now = now_ms()
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO guard_events(team_id,agent_id,session_id,tool,command,decision,reason,rule,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (team_id, agent_id, session_id, tool, cmd[:2000], decision, reason, rule, now),
        )
        if decision == "deny":
            await events.append(
                tx, team_id, events.GUARD_BLOCKED, "guard", None, agent_id,
                {"command": cmd[:200], "reason": reason, "rule": rule, "session_id": session_id},
            )
    return {"decision": decision, "reason": reason, "rule": rule}


async def add_rule(
    team_id: str, kind: str, pattern: str, pattern_type: str = "substring", reason: str | None = None
) -> dict[str, Any]:
    if kind not in ("allow", "deny"):
        return {"ok": False, "error": "kind must be allow|deny"}
    if pattern_type not in ("substring", "regex"):
        return {"ok": False, "error": "pattern_type must be substring|regex"}
    if not (pattern or "").strip():
        return {"ok": False, "error": "empty pattern"}
    if pattern_type == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            return {"ok": False, "error": f"bad regex: {e}"}
    async with db.transaction() as tx:
        cur = await tx.execute(
            "INSERT INTO guard_rules(team_id,kind,pattern,pattern_type,reason,enabled,created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (team_id, kind, pattern, pattern_type, reason, now_ms()),
        )
        rid = int(cur.lastrowid)
    return {"ok": True, "id": rid, "kind": kind, "pattern": pattern, "pattern_type": pattern_type}


async def list_rules(team_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id,kind,pattern,pattern_type,reason,enabled,created_at FROM guard_rules "
        "WHERE team_id=? ORDER BY id",
        (team_id,),
    )
    return [{k: r[k] for k in r.keys()} for r in rows]


async def delete_rule(team_id: str, rule_id: int) -> dict[str, Any]:
    async with db.transaction() as tx:
        cur = await tx.execute(
            "DELETE FROM guard_rules WHERE team_id=? AND id=?", (team_id, int(rule_id))
        )
        return {"ok": cur.rowcount == 1}


async def recent_events(team_id: str, limit: int = 100, decision: str | None = None) -> list[dict[str, Any]]:
    clauses = ["team_id=?"]
    params: list[Any] = [team_id]
    if decision in ("allow", "deny"):
        clauses.append("decision=?")
        params.append(decision)
    params.append(max(1, min(int(limit or 100), 500)))
    rows = await db.fetchall(
        "SELECT id,agent_id,session_id,tool,command,decision,reason,rule,ts FROM guard_events "
        f"WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        params,
    )
    return [{k: r[k] for k in r.keys()} for r in rows]


async def prune_expired(retention_ms: int) -> int:
    cutoff = now_ms() - retention_ms
    async with db.transaction() as tx:
        cur = await tx.execute("DELETE FROM guard_events WHERE ts < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
