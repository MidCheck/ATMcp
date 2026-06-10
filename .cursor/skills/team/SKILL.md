---
name: team
description: Manage the ATMcp agent team from this window — list agents and tasks, send directives, watch results, tail output. Use when the user types /team or asks to manage the team.
---

# /team — agent team console

You are the **console** for team **rc-superstars** via ATMcp MCP. Parse the `/team` argument; keep replies terse.

If `{not_joined}`, rely on MCP headers auto-join or `join_team(team_name="rc-superstars", display_name="cursor")`.

## Subcommands

- **`/team status`** or **`/team list`** — `get_team_status`, `list_agents`, `list_tasks`
- **`/team todo`** — `list_tasks` grouped by status
- **`/team send <agent> <instruction>`** — `send_directive(to_agent=..., instruction=...)`; print `directive_id`
- **`/team watch <directive_id>`** — `wait_directive(..., wait_ms=25000)` until terminal
- **`/team logs <agent>`** [`--follow`] — `get_agent_output(agent=..., since_seq=..., wait_ms=25000)`
- **`/team dispatch <instruction>`** — `create_task(title=...)` for any free worker
- **`/team directives [sent|received]`** — `list_directives(role=...)`

Workers need `/atmcp-worker` (or `/loop /atmcp-worker`) running on the target agent.
