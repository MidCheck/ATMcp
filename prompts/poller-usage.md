# `atmcp_worker_poller.py` — full usage reference

A **token-free worker**: it long-polls an agent's directive inbox over plain HTTP (zero model
tokens while idle) and invokes the model **only when a directive actually arrives**. By default it
keeps **one resumable session per worker** (memory across directives), **meters token/cost** and
enforces a **budget brake**, and **retries transient upstream-API failures** with backoff. Pure
Python stdlib — no dependencies.

```
idle           one HTTP request per ~30s, no model tokens
notice latency milliseconds (the inbox long-poll returns the instant a directive is sent)
per directive  claim → run claude -p (resumed) → report result + stream output + meter usage
```

## Prerequisites
- The `claude` CLI on PATH with the atmcp MCP server configured (so the executor can use team tools).
- A team + its **join token**; `--name` must match the name the console addresses (`/team send <name> …`).

## Quick start
```bash
python scripts/atmcp_worker_poller.py \
  --url http://<host>:8000 --team <team> --token <join_token> --name bob
```
Add `--dry-run` to exercise the loop without spending any model tokens.

## All options
Every option falls back to an env var (where listed); CLI flags win over env.

| Flag | Env | Default | What it does |
|---|---|---|---|
| `--url` | `ATMCP_URL` | `http://localhost:8000` | ATMcp server base URL |
| `--team` | `ATMCP_TEAM` | **required** | team name |
| `--token` | `ATMCP_TOKEN` | **required** | **join token** (write-capable) |
| `--name` | `ATMCP_NAME` | **required** | this worker's display name (the console addresses it by this) |
| `--model` | `ATMCP_MODEL` | `opus` | executor model for the `claude` executor (`opus`/`sonnet`/`haiku`) |
| `--session-mode` | — | `resume` | `resume` = one session per worker (memory); `fresh` = new session each directive |
| `--executor-cmd` | `ATMCP_EXECUTOR_CMD` | (claude) | custom executor template with a `{prompt}` token, e.g. `"codex exec --last {prompt}"` |
| `--workdir` | `ATMCP_WORKDIR` | — | base dir; a custom executor runs in `<workdir>/<name>` (for cwd-based resume) |
| `--state-dir` | `ATMCP_STATE_DIR` | `~/.atmcp` | per-worker state (session id + cumulative usage) — survives restarts |
| `--allowed-tools` | `ATMCP_ALLOWED` | `mcp__atmcp,Read,Edit,Bash,Write,Grep,Glob` | passed to `claude -p --allowedTools`; tighten to restrict the executor |
| `--claude-args` | `ATMCP_CLAUDE_ARGS` | `""` | extra flags forwarded to `claude -p` (shlex-split), e.g. `--add-dir /repo --permission-mode acceptEdits` |
| `--resume-session` | `ATMCP_RESUME_SESSION` | — | seed this worker from an **existing** claude session id at startup |
| `--cost-budget` | `ATMCP_COST_BUDGET` | `0` | USD budget; at the cap the worker **pauses** (stops claiming). `0` = unlimited |
| `--token-budget` | `ATMCP_TOKEN_BUDGET` | `0` | total (input+output) token budget; pauses when reached. `0` = unlimited |
| `--reset-usage` | — | off | zero this worker's cumulative cost/token counters at startup |
| `--max-retries` | `ATMCP_MAX_RETRIES` | `3` | retries on a **transient** failure (overload/429/5xx/network/timeout); `0` = no retry |
| `--retry-backoff` | `ATMCP_RETRY_BACKOFF` | `2.0` | base seconds for backoff (2/4/8…capped 60, jittered) |
| `--wait-ms` | — | `30000` | inbox long-poll window (ms); a directive returns instantly regardless |
| `--idle-sleep` | — | `1.0` | seconds to sleep after an empty poll |
| `--executor-timeout` | — | `1800.0` | per-directive execution timeout (s) |
| `--dry-run` | — | off | run the loop without invoking the model |

> ⚠️ Don't put `--model / --resume / --output-format / --allowedTools` in `--claude-args` — the
> poller manages those (via `--model`, the session logic, and `--allowed-tools`). And don't pass
> `--resume` yourself; the poller resumes automatically.

## Behavior notes
- **Memory (default `resume`):** captures the Claude `session_id` (`--output-format json`) and
  `--resume`s it each directive, so the worker remembers prior tasks while idle polling stays
  token-free. Session ids persist under `--state-dir`. Use `fresh` for stateless tasks. (This is a
  *single-agent, single-machine* memory; cross-agent shared memory is the ATMcp `memory`/`knowledge`
  tools.)
- **Token & cost:** parses `usage`/`total_cost_usd` from `claude -p` and reports it — the dashboard
  **Tokens** tab shows each agent's tokens/cost + rolling 5h/7d windows. The budget brake
  (`--cost-budget`/`--token-budget`) pauses the worker at the cap (cumulative totals persist per
  worker, surviving restarts); raise the budget or pass `--reset-usage` to resume. Custom executors
  emit no token JSON, so they report no usage.
- **Transient-failure retry:** a transient hiccup (API overload/429/5xx/network/timeout) is retried
  with jittered exponential backoff, **resuming the session so the model continues** (this resume
  holds even in `fresh` mode, just for the retry, so side effects aren't re-run). Permanent failures
  (auth, a guard-blocked command, a bad instruction or genuinely wrong result) are reported failed
  at once — never retried. A custom `--executor-cmd` must handle its own resume.
- **Latency:** the inbox long-poll returns the instant a directive is sent — don't use `/loop`
  (1-min cron). Serial: one directive at a time, so a worker never double-runs.

## Examples
```bash
# 1) Env-driven (handy for autostart/systemd)
export ATMCP_URL=http://192.168.2.7:18000 ATMCP_TEAM=my-team ATMCP_TOKEN=<jt> ATMCP_NAME=bob
python scripts/atmcp_worker_poller.py

# 2) Extra claude flags (more working dirs + permission mode)
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --claude-args "--add-dir /repo --add-dir /shared --permission-mode acceptEdits"

# 3) Hard budget brake (pause at $5 or 5M tokens), cheaper model
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --model sonnet --cost-budget 5 --token-budget 5000000

# 4) Aggressive retry for a flaky network
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --max-retries 5 --retry-backoff 3

# 5) Seed from an existing claude session
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --resume-session 1f2e3d4c-....

# 6) Codex / Cursor executor (their own resume flag)
python scripts/atmcp_worker_poller.py --team my-team --token <jt> --name bob \
  --executor-cmd "codex exec --last {prompt}" --workdir ~/atmcp-work
```

## Windows
Run it with the Python launcher; set env vars per shell:
```powershell
# PowerShell
$env:ATMCP_URL="http://<host>:18000"; $env:ATMCP_TEAM="my-team"
$env:ATMCP_TOKEN="<jt>"; $env:ATMCP_NAME="bob"
py scripts\atmcp_worker_poller.py
```
```bat
:: CMD
py scripts\atmcp_worker_poller.py --url http://<host>:18000 --team my-team --token <jt> --name bob
```
- **`claude` as a `.cmd` shim is handled automatically.** npm installs `claude` as `claude.cmd`,
  which a bare `subprocess` can't launch (WinError 2/193); the poller resolves it via PATHEXT and
  runs it through `cmd /c` (a real `claude.exe` is used directly). So both install methods work.
- **Custom `--executor-cmd` on Windows** is NOT cmd-wrapped (its prompt rides in argv, so shell-
  wrapping would be an injection risk). Point it at a real `.exe`, or run the poller under **WSL**.
- **Keep alive:** wrap as a service with **NSSM**, or a **Task Scheduler** task (trigger = at
  log-on/startup, "restart on failure"); or `Start-Process py -ArgumentList "…" -WindowStyle Hidden`.

## Run several / keep alive
- **Multiple workers:** one process per name (bob, alice…); each is serial and never claims another's directive.
- **Keep alive:** the script loops forever and retries network errors; wrap it in `nohup … &` / tmux / systemd / pm2 to survive process exit.
- **Stop:** `Ctrl-C`.

`python scripts/atmcp_worker_poller.py --help` prints this same flag set at any time.
