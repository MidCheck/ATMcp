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

## 2. Start a worker — pick a mode

### Mode A — token-efficient poller (recommended)

A plain script long-polls the inbox over HTTP and invokes a model **only when there is actual
work**, so idle time costs **zero tokens**. (An in-agent loop instead pays a full model turn —
system prompt + ~33 MCP tool schemas — on *every* poll just to discover an empty inbox; that
adds up to millions of wasted tokens a day.)

```bash
python scripts/atmcp_worker_poller.py \
  --url http://<host>:8000 --team <team> --token <join_token> --name bob --model opus
```

The poller heartbeats (presence + registers the name), long-polls the inbox, and on a directive
it claims → runs `claude -p --model <model> "<instruction>"` → reports the result and streams
output. The executor model runs **per directive only**. `--dry-run` tests the loop without
calling the model. Needs the `claude` CLI on PATH with the atmcp MCP server configured (so the
executor can use team tools when a directive needs them).

### Mode B — in-agent loop (simpler, but pays tokens per poll)

Run the `atmcp-worker` skill as an agent. Configure its MCP client with team headers:

```bash
claude mcp add --transport http atmcp http://<host>:8000/mcp \
  --header "Authorization: Bearer <join_token>" --header "X-ATMcp-Agent: bob"
```

Keep it running with the runner (fast poller model + Opus `atmcp-executor` subagent for the work):

```bash
ATMCP_MODEL=haiku ./scripts/atmcp_worker_runner.sh        # macOS/Linux
# Windows PowerShell:  $env:ATMCP_MODEL="haiku"; ./scripts/atmcp_worker_runner.ps1
```

> Avoid bare `/loop /atmcp-worker` (dynamic mode can silently stop, esp. Windows PowerShell —
> use `/loop 30s` or the runner). To cut cost in this mode: use a cheap `--model`, widen the
> poll/sleep interval, and keep the tool surface small. **Mode A avoids the per-poll cost entirely.**

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
