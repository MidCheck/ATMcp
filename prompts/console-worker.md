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
it claims → runs the executor → reports the result and streams output. The executor model runs
**per directive only**. `--dry-run` tests the loop without calling the model. Needs the `claude`
CLI on PATH with the atmcp MCP server configured (so the executor can use team tools).

**Memory across tasks (default).** The poller runs `--session-mode resume`: it keeps ONE session
per worker — captures the Claude `session_id` (`--output-format json`) and `--resume`s it on every
directive — so the worker remembers prior directives and Claude auto-compacts the context. Memory
persists while idle polling stays token-free (the watcher polls, not the model). Session ids are
saved under `--state-dir` (default `~/.atmcp`) so they survive poller restarts. Use
`--session-mode fresh` for stateless tasks. For Codex/Cursor, plug their resume flag:
`--executor-cmd "codex exec --last {prompt}"` or `"cursor-agent -p --resume {prompt}"` (run with
`--workdir <dir>`; note Cursor headless resume is unreliable as of early 2026).

**Extra `claude` flags.** Don't pass `--resume` yourself — the poller manages it. For any other
flag (e.g. extra working dirs, permission mode, an mcp config), use `--claude-args`, which is
shlex-split and forwarded to `claude -p`:
```bash
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --claude-args "--add-dir /repo --add-dir /shared --permission-mode acceptEdits"
```
Don't put `--model / --resume / --output-format / --allowedTools` in `--claude-args` (the poller
sets those via `--model`, the session logic, and `--allowed-tools`). To pick up an *existing*
Claude session at startup, use `--resume-session <id>`.

**Token & cost.** The poller parses `claude -p --output-format json`'s `usage`/`total_cost_usd`
(free — it already reads that JSON for the session id) and reports it, so the dashboard **Tokens**
tab shows each agent's tokens/cost + rolling 5h/7d windows. Set a hard brake with
`--cost-budget <USD>` and/or `--token-budget <N>` (0 = unlimited): when cumulative spend (persisted
per worker in `--state-dir`, surviving restarts) reaches it, the worker **stops claiming** and shows
"paused: budget reached". Raise the budget or pass `--reset-usage` to resume. Custom `--executor-cmd`
tools don't emit that JSON, so they report no token data.

**Transient-failure retry.** If a directive fails on a *transient* upstream hiccup (API overload /
429 / 5xx / network / timeout) the poller retries it with exponential backoff (`--max-retries`,
default 3; `--retry-backoff`, default 2s → 2/4/8…capped 60, jittered), **resuming the same session**
so the model continues rather than redoing work. **Permanent** failures (auth, a guard-blocked
command, a bad instruction, or a genuinely wrong result) are reported failed immediately — never
retried, so you don't burn tokens on a doomed run. Each attempt's tokens are still metered and count
toward the budget; retries stop early if the budget is reached. `--max-retries 0` disables it.

**Latency.** The inbox long-poll returns the *instant* a directive is sent (≈ms) — so don't use
`/loop` (1-minute cron granularity). End-to-end ≈ executor spin-up + resume load (a few seconds).
Process one directive at a time (the poller is serial), so a single worker never double-runs.

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
