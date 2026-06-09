"""The FastMCP server: the remote (streamable-HTTP) tool surface agents call.

Identity is resolved from the MCP session (bound at join_team), so scoped tools take
no team/agent arguments. Tools return plain dicts (serialized as JSON to the agent).
Expected conditions (conflict, taken, stale_token, not_joined) are returned as data,
not raised, so the calling LLM can branch on them.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from atmcp import hub, session
from atmcp.services import directives as directives_svc
from atmcp.services import identity as identity_svc
from atmcp.services import knowledge as knowledge_svc
from atmcp.services import output as output_svc
from atmcp.services import memory as memory_svc
from atmcp.services import presence as presence_svc
from atmcp.services import status as status_svc
from atmcp.services import tasks as tasks_svc

INSTRUCTIONS = """\
ATMcp lets your team of agents collaborate across devices. First call `join_team` with
your team name + join token and a `display_name`. Then call `heartbeat` every ~10s so the
team sees you online. Share findings with `post_knowledge`/`search_knowledge`, shared state
with `set_memory`/`get_memory`, and coordinate work with `create_task`, `claim_next_task`,
`update_task_progress`, and `complete_task`. Use `get_team_status`/`list_tasks`/`list_agents`
to see the team, and `sync(since_event_id, wait_ms)` to catch up on what changed.
"""

mcp = FastMCP("ATMcp", instructions=INSTRUCTIONS, streamable_http_path="/")


async def _resolve(ctx: Context) -> tuple[session.Caller | None, dict[str, Any] | None]:
    try:
        return await session.resolve(ctx), None
    except session.NotJoinedError as exc:
        return None, {"ok": False, "error": "not_joined", "hint": str(exc)}


# ── team / identity ─────────────────────────────────────────────────────────
@mcp.tool()
async def join_team(
    ctx: Context,
    team_name: str,
    display_name: str,
    join_token: str | None = None,
    capabilities: list[str] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Join (or re-join) a team. The join token may be passed here or sent as an
    `Authorization: Bearer <token>` / `X-ATMcp-Token` header by your MCP client."""
    sid = session.session_id_from_ctx(ctx)
    if not sid:
        return {"ok": False, "error": "no_session", "hint": "use a streamable-http MCP client"}
    token = join_token or session.header_token_from_ctx(ctx)
    if not token:
        return {"ok": False, "error": "missing_join_token"}
    try:
        return await identity_svc.join_team(sid, team_name, token, display_name, capabilities, agent_id)
    except identity_svc.UnknownTeamError:
        return {"ok": False, "error": "unknown_team", "team_name": team_name}
    except identity_svc.BadTokenError:
        return {"ok": False, "error": "invalid_join_token"}
    except identity_svc.DisplayNameTakenError:
        return {"ok": False, "error": "display_name_taken", "display_name": display_name}


@mcp.tool()
async def leave_team(ctx: Context) -> dict[str, Any]:
    """Gracefully leave the team: releases held tasks and marks you offline."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await identity_svc.leave_team(caller)


# ── presence ────────────────────────────────────────────────────────────────
@mcp.tool()
async def heartbeat(
    ctx: Context,
    status_summary: str | None = None,
    current_task_id: str | None = None,
    progress_pct: int | None = None,
) -> dict[str, Any]:
    """Refresh your presence (~every 10s). Also renews any task lease you hold and
    pushes your status/progress to the dashboard."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await presence_svc.heartbeat(caller, status_summary, current_task_id, progress_pct)


# ── knowledge ───────────────────────────────────────────────────────────────
@mcp.tool()
async def post_knowledge(
    ctx: Context,
    title: str,
    body: str,
    tags: list[str] | None = None,
    task_id: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Share a finding. Identical content (same title/body/tags) is auto-deduped;
    `contributor_count` tracks how many agents independently contributed it."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await knowledge_svc.post_knowledge(caller, title, body, tags, task_id, idem_key)


@mcp.tool()
async def search_knowledge(
    ctx: Context,
    query: str | None = None,
    tags: list[str] | None = None,
    since_event_id: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Full-text search the team knowledge base (FTS5). Omit `query` to list recent
    entries; pass `since_event_id` for incremental sync; `tags` filters by all tags."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    items = await knowledge_svc.search_knowledge(caller, query, tags, since_event_id, limit)
    return {"ok": True, "count": len(items), "items": items}


@mcp.tool()
async def retract_knowledge(ctx: Context, content_id: str, idem_key: str | None = None) -> dict[str, Any]:
    """Tombstone a knowledge entry (OR-Set remove). A concurrent re-post wins (add-bias)."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await knowledge_svc.retract_knowledge(caller, content_id, idem_key)


# ── memory ──────────────────────────────────────────────────────────────────
@mcp.tool()
async def set_memory(
    ctx: Context,
    key: str,
    value: Any,
    expected_version: int | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Set a shared-memory key (LWW by team logical clock). Pass `expected_version`
    for optimistic CAS — on mismatch you get `{conflict, current_value, current_version}`."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await memory_svc.set_memory(caller, key, value, expected_version, idem_key)


@mcp.tool()
async def get_memory(ctx: Context, key: str | None = None) -> dict[str, Any]:
    """Read a shared-memory key, or all keys if `key` is omitted."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await memory_svc.get_memory(caller, key)


# ── goals, tasks & progress ─────────────────────────────────────────────────
@mcp.tool()
async def create_goal(ctx: Context, title: str, description: str | None = None) -> dict[str, Any]:
    """Create a team goal that tasks can roll up into."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.create_goal(caller, title, description)


@mcp.tool()
async def create_task(
    ctx: Context,
    title: str,
    description: str | None = None,
    goal_id: str | None = None,
    parent_id: str | None = None,
    priority: int = 0,
    weight: int = 1,
    depends_on: list[str] | None = None,
    lease_ttl_s: int | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Create a task. `depends_on` lists task_ids that must be done first; `weight`
    feeds the goal rollup; `priority` orders `claim_next_task`."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.create_task(
        caller, title, description, parent_id, goal_id, priority, weight, depends_on, lease_ttl_s, idem_key
    )


@mcp.tool()
async def claim_task(ctx: Context, task_id: str, idem_key: str | None = None) -> dict[str, Any]:
    """Atomically claim a specific open task. Returns a `fencing_token` you must present
    on every later update/complete/fail/release. `{taken_by}` if someone else has it."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.claim_task(caller, task_id, idem_key)


@mcp.tool()
async def claim_next_task(
    ctx: Context, capability_filter: str | None = None, idem_key: str | None = None
) -> dict[str, Any]:
    """Claim the highest-priority eligible (deps satisfied) open task in one round-trip."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.claim_next_task(caller, capability_filter, idem_key)


@mcp.tool()
async def update_task_progress(
    ctx: Context,
    task_id: str,
    fencing_token: int,
    progress_pct: int | None = None,
    status: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Report progress and renew your lease. `status` may be 'in_progress' or 'blocked'.
    A stale `fencing_token` is rejected with `{stale_token: true}`."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.update_task_progress(caller, task_id, fencing_token, progress_pct, status, note)


@mcp.tool()
async def complete_task(
    ctx: Context,
    task_id: str,
    fencing_token: int,
    result_summary: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Mark a task done. Returns `eligible_unblocked` — downstream tasks now claimable."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.complete_task(caller, task_id, fencing_token, result_summary, idem_key)


@mcp.tool()
async def fail_task(ctx: Context, task_id: str, fencing_token: int, error: str | None = None) -> dict[str, Any]:
    """Report a task failed. Re-queued (open) until max_attempts, then dead-lettered (failed)."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.fail_task(caller, task_id, fencing_token, error)


@mcp.tool()
async def release_task(ctx: Context, task_id: str, fencing_token: int) -> dict[str, Any]:
    """Voluntarily give a task back to the queue (instant re-queue, no lease wait)."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await tasks_svc.release_task(caller, task_id, fencing_token)


@mcp.tool()
async def list_tasks(
    ctx: Context,
    status: str | None = None,
    assignee: str | None = None,
    goal_id: str | None = None,
    since_event_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """List the task board, optionally filtered by status/assignee/goal or since a cursor."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    items = await tasks_svc.list_tasks(caller.team_id, status, assignee, goal_id, since_event_id, limit)
    return {"ok": True, "count": len(items), "tasks": items}


# ── status / query / sync ───────────────────────────────────────────────────
@mcp.tool()
async def list_agents(ctx: Context) -> dict[str, Any]:
    """Team roster with derived presence (healthy/degraded/offline), status, and progress."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    agents = await identity_svc.list_agents(caller.team_id)
    return {"ok": True, "count": len(agents), "agents": agents}


@mcp.tool()
async def get_team_status(ctx: Context) -> dict[str, Any]:
    """One-shot snapshot: agents online, task counts, goal progress %, knowledge count, head cursor."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await status_svc.get_team_status(caller.team_id)


@mcp.tool()
async def sync(
    ctx: Context, since_event_id: int = 0, wait_ms: int = 0, limit: int = 200
) -> dict[str, Any]:
    """Catch up on events since a cursor. If nothing new and `wait_ms` > 0, long-poll up to
    that many ms (capped 30s) for the next event before returning."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    # Arm the generation BEFORE the DB read so an event landing in the gap can't be missed.
    gen0 = hub.current_gen(caller.team_id)
    res = await status_svc.sync(caller.team_id, since_event_id, limit)
    if res["count"] == 0 and wait_ms and wait_ms > 0:
        changed = await hub.wait_for_change(caller.team_id, gen0, min(int(wait_ms), 30000) / 1000.0)
        if changed:
            res = await status_svc.sync(caller.team_id, since_event_id, limit)
    return res


# ── directives (console → a specific agent) ─────────────────────────────────
@mcp.tool()
async def send_directive(
    ctx: Context,
    to_agent: str,
    instruction: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Send a command to ONE specific agent (by display_name or agent_id). The target picks
    it up via `inbox` and reports back; use `wait_directive` to await the result."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.send_directive(caller, to_agent, instruction, payload, priority, idem_key)


@mcp.tool()
async def inbox(ctx: Context, wait_ms: int = 0, limit: int = 20, include_running: bool = False) -> dict[str, Any]:
    """Your directive inbox: pending commands addressed to you. With `wait_ms` > 0 this
    long-polls (up to 30s) until a directive arrives — ideal for a worker loop."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.inbox(caller, wait_ms, limit, include_running)


@mcp.tool()
async def claim_directive(ctx: Context, directive_id: str) -> dict[str, Any]:
    """Mark a directive addressed to you as 'running' before you start executing it."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.claim_directive(caller, directive_id)


@mcp.tool()
async def report_directive(
    ctx: Context,
    directive_id: str,
    status: str,
    result_summary: str | None = None,
    output: str | None = None,
    idem_key: str | None = None,
) -> dict[str, Any]:
    """Report a directive finished: status must be 'done' or 'failed'. The issuer's
    `wait_directive` unblocks with your `result_summary` / `output`."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.report_directive(caller, directive_id, status, result_summary, output, idem_key)


@mcp.tool()
async def wait_directive(ctx: Context, directive_id: str, wait_ms: int = 0) -> dict[str, Any]:
    """Await a directive you sent. With `wait_ms` > 0, long-polls until it reaches a terminal
    state (done/failed/canceled). Returns the directive incl. result_summary/result_output."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.wait_directive(caller, directive_id, wait_ms)


@mcp.tool()
async def cancel_directive(ctx: Context, directive_id: str) -> dict[str, Any]:
    """Cancel a directive you issued (only if it hasn't finished)."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await directives_svc.cancel_directive(caller, directive_id)


@mcp.tool()
async def list_directives(
    ctx: Context, role: str | None = None, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """List directives. role='sent' (issued by you), 'received' (addressed to you), or omit
    for the whole team; optionally filter by status."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    items = await directives_svc.list_directives(caller, role, status, limit)
    return {"ok": True, "count": len(items), "directives": items}


# ── agent output stream (view what an agent is printing) ────────────────────
@mcp.tool()
async def append_output(ctx: Context, text: str, directive_id: str | None = None) -> dict[str, Any]:
    """Stream a chunk of YOUR output/progress so the console can watch it live (optionally
    tagged with the directive_id you're working on)."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    return await output_svc.append_output(caller.team_id, caller.agent_id, text, directive_id, source="agent")


@mcp.tool()
async def get_agent_output(
    ctx: Context, agent: str, since_seq: int = 0, wait_ms: int = 0, limit: int = 200
) -> dict[str, Any]:
    """Tail another agent's output (by display_name or agent_id) from `since_seq`. With
    `wait_ms` > 0, long-polls for the next chunk. Returns chunks + `head_seq` to continue."""
    caller, err = await _resolve(ctx)
    if err:
        return err
    agent_id = await identity_svc.resolve_agent_ref(caller.team_id, agent)
    if agent_id is None:
        return {"ok": False, "error": "unknown_agent", "agent": agent}
    return await output_svc.get_output(caller.team_id, agent_id, since_seq, wait_ms, limit)
