"""Server-side `/team` command runner for the dashboard console box.

The dashboard isn't an LLM — it sends a raw command string here and we execute it against
the existing services as a synthetic "console" identity (from_agent = console:<name>, no
roster entry). Returns a structured {ok, kind, message, data} the UI renders as a chat reply.
Live updates (directive done, new output) still arrive over the dashboard WebSocket.
"""

from __future__ import annotations

from typing import Any

from atmcp.services import directives as d
from atmcp.services import identity as ident
from atmcp.services import output as o
from atmcp.services import status as st
from atmcp.services import tasks as t
from atmcp.session import Caller

HELP = (
    "commands (the leading /team is optional):\n"
    "  status                       — roster + task summary\n"
    "  todo                         — task board\n"
    "  send <agent> <instruction>   — directive to a specific agent\n"
    "  dispatch <instruction>       — task for whoever's free\n"
    "  watch <directive_id>         — a directive's current result\n"
    "  logs <agent>                 — recent output of an agent\n"
    "  directives [sent|received]   — list directives\n"
    "  cancel <directive_id>        — cancel a directive you issued"
)


def _caller(team_id: str, console_name: str) -> Caller:
    # Synthetic identity — the console is a sender, not a roster worker.
    return Caller(team_id, f"console:{console_name}", console_name, f"rest-console:{console_name}")


async def run_command(team_id: str, console_name: str, raw: str) -> dict[str, Any]:
    caller = _caller(team_id, console_name)
    cmd = (raw or "").strip()
    for p in ("/team", "/"):
        if cmd.startswith(p):
            cmd = cmd[len(p):].strip()
    if not cmd:
        cmd = "help"
    parts = cmd.split(None, 1)
    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if verb in ("help", "?", "h"):
        return {"ok": True, "kind": "help", "message": HELP}

    if verb in ("status", "list", "agents"):
        snap = await st.get_team_status(team_id)
        agents = await ident.list_agents(team_id)
        tasks = await t.list_tasks(team_id, limit=200)
        msg = (f"{snap['agents_online']}/{snap['agents_total']} online · goal "
               f"{snap['goal_progress_pct']}% · tasks {snap['tasks']} · "
               f"{snap['knowledge_count']} knowledge")
        return {"ok": True, "kind": "status", "message": msg,
                "data": {"status": snap, "agents": agents, "tasks": tasks}}

    if verb == "todo":
        tasks = await t.list_tasks(team_id, limit=300)
        by: dict[str, list] = {}
        for task in tasks:
            by.setdefault(task["status"], []).append(task)
        msg = " · ".join(f"{k}:{len(v)}" for k, v in by.items()) or "no tasks"
        return {"ok": True, "kind": "todo", "message": msg, "data": {"tasks": tasks}}

    if verb == "send":
        sub = rest.split(None, 1)
        if len(sub) < 2:
            return {"ok": False, "kind": "usage", "message": "usage: send <agent> <instruction>"}
        to_agent, instruction = sub[0], sub[1]
        r = await d.send_directive(caller, to_agent, instruction)
        if not r.get("ok"):
            return {"ok": False, "kind": "send", "message": f"send failed: {r.get('error')}", "data": r}
        return {"ok": True, "kind": "send",
                "message": f"→ sent directive {r['directive_id'][-6:]} to {to_agent}", "data": r}

    if verb == "dispatch":
        if not rest:
            return {"ok": False, "kind": "usage", "message": "usage: dispatch <instruction>"}
        r = await t.create_task(caller, rest)
        return {"ok": True, "kind": "dispatch",
                "message": f"created task {r['task_id'][-6:]} (claimable by any worker)", "data": r}

    if verb == "watch":
        if not rest:
            return {"ok": False, "kind": "usage", "message": "usage: watch <directive_id>"}
        r = await d.wait_directive(caller, rest, wait_ms=0)
        if not r.get("ok"):
            return {"ok": False, "kind": "watch", "message": r.get("error", "unknown"), "data": r}
        dd = r["directive"]
        extra = f" — {dd['result_summary']}" if dd.get("result_summary") else ""
        return {"ok": True, "kind": "watch",
                "message": f"directive {dd['directive_id'][-6:]}: {dd['status']}{extra}", "data": dd}

    if verb == "cancel":
        if not rest:
            return {"ok": False, "kind": "usage", "message": "usage: cancel <directive_id>"}
        r = await d.cancel_directive(caller, rest)
        return {"ok": r.get("ok", False), "kind": "cancel",
                "message": ("canceled " + rest[-6:]) if r.get("ok") else r.get("error", "failed"),
                "data": r}

    if verb == "logs":
        agent = rest.split(None, 1)[0] if rest else ""
        if not agent:
            return {"ok": False, "kind": "usage", "message": "usage: logs <agent>"}
        aid = await ident.resolve_agent_ref(team_id, agent)
        if aid is None:
            return {"ok": False, "kind": "logs", "message": f"unknown agent: {agent}"}
        out = await o.get_output(team_id, aid, 0, 0, 200)
        return {"ok": True, "kind": "logs",
                "message": f"{out['count']} output chunk(s) from {agent}",
                "data": {"agent": agent, "agent_id": aid, **out}}

    if verb == "directives":
        role = rest.strip().lower()
        role = role if role in ("sent", "received") else None
        items = await d.list_directives(caller, role)
        return {"ok": True, "kind": "directives",
                "message": f"{len(items)} directive(s)", "data": {"directives": items}}

    return {"ok": False, "kind": "unknown", "message": f"unknown command: {verb} — try 'help'"}
