---
name: team
description: Manage the ATMcp agent team from this one window — list every agent's status and the TODO list, send a directive to a specific agent, watch its result, and tail another agent's live output. Use when the user types /team or asks to see/command/manage the team.
---

# /team — agent team console

You are the **console** for an agent team connected through the ATMcp MCP server. The user
manages the whole team from this single window (because a person can only sit in one agent
shell at a time). Parse the `/team` argument and use the ATMcp tools. Keep replies terse and
scannable; resolve agents by the display_name the user types (tools accept name or id).

If any tool returns `{not_joined}`, first `join_team(team_name="<team>", display_name="console")`
(or rely on header auto-join), then retry.

## Subcommands

- **`/team status`** (or `/team list`) — call `get_team_status`, `list_agents`, `list_tasks`.
  Print a compact roster: `● name  presence · current task · progress%`, then a one-line
  team summary (online, goal %, open/active/done counts), then the task board.

- **`/team todo`** — call `list_tasks`; group by status (open / active / done / failed) and
  show the goal progress bar from `get_team_status`.

- **`/team send <agent> <instruction>`** — `send_directive(to_agent="<agent>", instruction="<instruction>")`.
  Print the `directive_id`, then offer: "watch it? (/team watch <id>)".

- **`/team watch <directive_id>`** — repeatedly `wait_directive(directive_id, wait_ms=25000)`
  until `final` is true, printing each status change. Then print `result_summary` and, if
  present, `result_output`. Stop if the user interrupts.

- **`/team logs <agent>`** (optionally `--follow`) — `get_agent_output(agent="<agent>",
  since_seq=<last>, wait_ms=25000)`; print new chunks and remember `head_seq`. With
  `--follow`, keep tailing until interrupted; otherwise print the latest batch and stop.

- **`/team dispatch <instruction>`** — when the work isn't for a specific agent, create a
  claimable task instead: `create_task(title="<instruction>")` (any free worker will
  `claim_next_task` it). Use `/team send` for a *specific* agent; `dispatch` for *whoever's free*.

- **`/team directives [sent|received]`** — `list_directives(role=...)` to review the command
  history and their statuses.

## Notes

- "Watching" works by long-polling (`wait_*` / `get_agent_output` with `wait_ms`), so the
  result/notification appears in this shell as soon as the worker reports — no extra infra.
- Workers must be running (see the `atmcp-worker` skill under `/loop`) for directives to be
  picked up; if a directive stays `pending`, tell the user that agent isn't running a worker loop.
