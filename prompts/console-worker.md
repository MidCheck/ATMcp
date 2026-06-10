# Team console + workers — manage the whole team from one window

Pattern: **one interactive console** window (where you sit) + **N background worker loops**.
From the console you list everyone's status & TODOs, send a directive to a *specific* agent,
watch its result, and tail another agent's live output — solving "a person can only be in one
agent shell at a time".

```
  You ── /team ──►  Console agent ──MCP──►  ATMcp  ◄──MCP── Worker agents (/loop /atmcp-worker)
        commands     send_directive ───────► directives ──► inbox → claim → execute
        results  ◄── wait_directive  ◄─────── (status)  ◄── report_directive
        output   ◄── get_agent_output ◄────── agent_output ◄ append_output / hook
```

## 1. Install the skills + executor subagent

```bash
cp -r skills/team skills/atmcp-worker ~/.claude/skills/   # console + worker skills
cp agents/atmcp-executor.md ~/.claude/agents/             # Opus executor subagent
# (or the project-level .claude/skills and .claude/agents)
```

## 2. Start a worker (one per agent, kept running reliably)

Configure the worker's MCP client with the team headers (auto-join + an addressable name):

```bash
claude mcp add --transport http atmcp http://<host>:8000/mcp \
  --header "Authorization: Bearer <join_token>" \
  --header "X-ATMcp-Agent: bob"
```

Keep it available with the **runner script** (recommended — survives any turn ending/crash,
runs the poller on a fast model and delegates heavy work to the Opus executor subagent):

```bash
ATMCP_MODEL=haiku ./scripts/atmcp_worker_runner.sh        # macOS/Linux
# Windows PowerShell:  $env:ATMCP_MODEL="haiku"; ./scripts/atmcp_worker_runner.ps1
```

> In-app alternative: `/loop 30s /atmcp-worker` (use an **explicit interval**). Avoid bare
> `/loop /atmcp-worker` — dynamic self-paced mode relies on the model re-arming each turn and
> can silently stop after a while (especially on Windows PowerShell). The runner script avoids
> this entirely. The **two-model split** (cheap poller + Opus `atmcp-executor` subagent) keeps
> polling cheap while the actual instruction is executed with full reasoning.

Optional but recommended — stable presence + real terminal-output capture:

```bash
# presence (stay "online" even while thinking)
python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name bob &

# output capture: add a Stop hook -> scripts/atmcp_output_hook.py in settings.json,
# with env ATMCP_URL / ATMCP_TEAM / ATMCP_TOKEN=<join_token> / ATMCP_NAME=bob
```

## 3. Drive the team from the console window

In your own Claude Code session (also connected to atmcp, display_name `console`):

```
/team status                  # roster (presence · task · progress) + TODO board
/team send bob "refactor X"   # directive to a specific agent → prints a directive_id
/team watch <directive_id>    # blocks until bob reports done/failed, prints the result
/team logs bob --follow       # live-tail bob's output
/team dispatch "fix flaky test"   # no specific agent → a claimable task for whoever's free
/team directives sent         # review the commands you've issued and their status
```

Under the hood: `/team` uses `send_directive` / `wait_directive` / `get_agent_output` /
`list_directives`; the worker loop uses `inbox` / `claim_directive` / `append_output` /
`report_directive`. "Watching" is long-poll, so the result/notification surfaces in your
console shell the moment the worker reports — no extra infrastructure.
