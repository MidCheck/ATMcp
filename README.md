# ATMcp — Agent Teams MCP Server

**English** · [中文文档](README.zh-CN.md)

A single, network-reachable **MCP server** that lets LLM coding agents (Claude Code and
any other MCP client) on **different devices / networks / regions** form a **team** and
work together — sharing **knowledge, memory, task goals, progress, and completion** — with
a **live web dashboard** showing every agent's status.

Built with **Python + FastAPI + Redis + SQLite**, drawing on distributed-systems patterns
(cloud presence/heartbeats, an append-only content-addressed log, CRDT-style merge
semantics, and lease-based task scheduling) — kept to the parts that earn their keep at MVP
scale.

```
 Remote agents (different devices/networks)        Browser
   Claude Code A   Claude Code B   …                Dashboard
        │  streamable-HTTP MCP  │                       │ HTTP + WebSocket
        ▼                       ▼                       ▼
   ┌──────────────────── one FastAPI process (uvicorn) ───────────────────┐
   │  /mcp  FastMCP(streamable-http)   /dashboard  /ws/{team}  /api/*       │
   │  SQLite (WAL) = source of truth · events log = audit/replay/feed        │
   │  in-proc hub → WebSocket fan-out · reaper → re-queue expired leases     │
   └───────────────────────────────────┬───────────────────────────────────┘
                                        ▼  soft state (rebuildable)
        Redis: heartbeats (presence TTL) · task leases · streams (catch-up/fan-out)
```

## Key properties

- **SQLite is the source of truth** (WAL, single in-process writer serialized by one lock).
  **Redis is soft state** — lose it and you lose only liveness (presence, live fan-out,
  lease-based re-queue), never durable data. Everything rebuilds from SQLite.
- **Commit-then-publish**: every mutation is `BEGIN IMMEDIATE → write → append one events
  row → COMMIT → fan out`. Tool responses are read-your-writes.
- **Presence is derived**, never stored: `online = heartbeat key exists` (30s TTL, ~10s
  refresh). A crashed/partitioned agent self-cleans on expiry.
- **Knowledge is content-addressed** (`sha256`): identical findings auto-dedupe and gain
  provenance (`contributor_count`); modeled as an OR-Set with a fast projection + FTS5 search.
- **Memory is a LWW-register** ordered by a per-team Lamport clock; optional
  `expected_version` gives optimistic CAS that returns conflicts as data.
- **Tasks use lease-based claiming**: a DB-arbitrated atomic claim + a Redis lease + a
  `fencing_token` means cross-device agents never duplicate work, and a 5s **reaper**
  re-queues work abandoned by a crashed agent. Zombies are rejected by their stale token.
- **Multi-tenant isolation is structural**: `team_id` leads every index and prefixes every
  Redis key; scoped tools derive the team from the join session, never from client input.

## Quick start (Docker)

```bash
cp .env.example .env            # set ATMCP_ADMIN_TOKEN
docker compose up --build       # starts redis + atmcp on :8000

# Create a team (returns its join token + URLs):
curl -s -X POST http://localhost:8000/api/teams \
  -H "X-Admin-Token: $ATMCP_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-team"}' | jq
```

## Quick start (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run_local.sh                  # starts a redis container + uvicorn on :8000

# Create a team without the HTTP API (writes straight to SQLite):
python -m atmcp.admin create-team my-team
```

## Connect an agent

Point any streamable-HTTP MCP client at `http://<host>:8000/mcp`, carrying the team join
token (and an optional stable agent name) in headers. With the headers set, the agent is
**auto-joined on its first tool call** — no explicit `join_team` needed.

```bash
# Claude Code
claude mcp add --transport http atmcp http://<host>:8000/mcp \
  --header "Authorization: Bearer <join_token>" \
  --header "X-ATMcp-Agent: alice"
```

> **Adding the server only makes the tools _available_.** Agents (Claude/Cursor/Qwen) won't
> report anything until told to — MCP is *pull, not push*, and LLMs have no timer. So:
> 1. Give each agent the workflow prompt — ready-to-paste rules for Claude Code / Cursor /
>    Qwen are in **[`prompts/`](prompts/)**.
> 2. For reliable presence (stay "online" even while only thinking), run the sidecar — it
>    heartbeats over REST, decoupled from the LLM:
>    ```bash
>    python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
>      --team <team> --token <join_token> --name alice --interval 10
>    ```

If your client can't set headers, the agent passes the token to `join_team` directly:
`join_team(team_name="my-team", display_name="alice", join_token="<join_token>")`.

## Dashboard

Open `http://<host>/dashboard?team=<team>` — a full-height **three-column** app (no page
scrolling; each column scrolls on its own):
- **Left:** goal progress + stats and the live agent roster (presence green/amber/grey).
- **Center (tabbed):** Task board · Activity feed · Knowledge — and **clicking an agent** opens
  an **Agent** tab right there with its directives, tasks, and live output (no scrolling down).
- **Right (always visible):** the **Team console** — type `/team` commands (a "pseudo-model"
  chat) with the input pinned at the bottom; results and live directive events stream in.

It loads a JSON snapshot then live-updates over a WebSocket (auto-reconnects with catch-up).
Auth is **off by default**; set `ATMCP_DASHBOARD_AUTH=1` to require a per-team read-only token
(the console always needs the join token to send commands).

## MCP tools

| Group | Tools |
|---|---|
| Identity | `join_team`, `leave_team` |
| Presence | `heartbeat` |
| Knowledge | `post_knowledge`, `search_knowledge`, `retract_knowledge` |
| Memory | `set_memory`, `get_memory` |
| Goals/Tasks | `create_goal`, `create_task`, `claim_task`, `claim_next_task`, `update_task_progress`, `complete_task`, `fail_task`, `release_task`, `list_tasks` |
| Directives | `send_directive`, `inbox`, `claim_directive`, `report_directive`, `wait_directive`, `cancel_directive`, `list_directives` |
| Output | `append_output`, `get_agent_output` |
| Status/Sync | `list_agents`, `get_team_status`, `sync` |

Every mutating tool accepts an optional `idem_key` (idempotency). Expected conditions are
returned as data (`{conflict}`, `{taken_by}`, `{stale_token}`, `{not_joined}`), not errors.
`sync(since_event_id, wait_ms)` long-polls for the next event so agents can react quickly.

## Configuration

See `.env.example`. Highlights: `ATMCP_ADMIN_TOKEN`, `ATMCP_SQLITE_PATH`, `ATMCP_REDIS_URL`,
`ATMCP_PUBLIC_URL`, heartbeat TTL/interval (`30`/`10s`), lease TTL (`90s`), reaper interval
(`5s`), `ATMCP_TASK_MAX_ATTEMPTS`, `ATMCP_DASHBOARD_AUTH`.

## Failure model (summary)

| Failure | Behavior |
|---|---|
| Agent crash / partition | heartbeat key expires → shown offline; held lease expires → reaper re-queues; last durable progress kept. |
| Agent reconnect | re-`join_team` (same `agent_id` via `(team, display_name)`) → `sync(since_event_id)` to catch up. |
| Duplicate / retried call | `idem_key` returns the stored result; identical knowledge auto-dedupes; stale fencing token rejected. |
| Redis down | mutations still commit to SQLite; presence falls back to `last_seen`; double-claim still prevented by the DB. |
| Server restart | SQLite intact (WAL); agents reconnect & re-join; reaper reconciles leases. |

## Testing

```bash
source .venv/bin/activate
pip install -r requirements.txt
pytest -q            # service-level tests: claim race, fencing/zombie, reaper,
                     # dedupe, LWW/CAS, isolation, REST snapshot
```

## Layout

```
atmcp/
  app.py            FastAPI assembly + lifespan (mounts /mcp, wires publisher)
  mcp_server.py     FastMCP tool surface (~33 tools)
  web.py            dashboard, /api/*, /ws/{team}, REST heartbeat/output, health, admin
  db.py             single-writer SQLite, transaction() = commit-then-publish
  redis_bus.py      soft state: heartbeats, leases, streams, sessions (best-effort)
  hub.py            in-process WebSocket fan-out + long-poll notify (generation counter)
  reaper.py         re-queues expired-lease tasks; prunes idempotency + agent output
  events.py         append to the monotonic events log
  idempotency.py    durable, in-transaction idempotency (retry-safe mutating tools)
  session.py        MCP-session → (team, agent) binding + header auto-join
  canonical.py      content-addressing (canonical JSON + sha256)
  schema.sql        full DDL (+ FTS5)
  services/         identity · presence · knowledge · memory · tasks · status · clock
                    · directives (console→agent commands) · output (agent output stream)
  static/           dashboard.html + dashboard.js
prompts/            ready-to-paste agent rules + console/worker setup
scripts/            atmcp_worker_poller.py (token-free worker) · atmcp_heartbeat.py
                    · atmcp_output_hook.py · atmcp_worker_runner.{sh,ps1}
skills/             team (console) + atmcp-worker (worker loop) Claude Code skills
agents/             atmcp-executor (Opus subagent the worker delegates execution to)
```

## Team console — manage the whole team from one window

One interactive **console** window + N background **worker** loops. From the console you list
everyone's status & TODOs, send a directive to a *specific* agent, watch its result, and tail
another agent's live output. See **[`prompts/console-worker.md`](prompts/console-worker.md)**
and the **[`skills/`](skills/)** (`/team` console + `/atmcp-worker` loop).

```
/team status                 # roster + TODO board
/team send bob "refactor X"  # → directive_id
/team watch <directive_id>   # long-polls until bob reports done/failed, prints result
/team logs bob --follow      # live-tail bob's output
```

Server-side this is the **directive bus** (`send_directive`/`inbox`/`claim_directive`/
`report_directive`/`wait_directive`) + the **agent output stream** (`append_output`/
`get_agent_output`, plus `POST /api/teams/{team}/agents/{agent}/output` for the hook).
"Watching" is long-poll, so results surface as soon as the worker reports.

**Run workers cheaply.** An in-agent loop pays a full model turn (system prompt + all tool
schemas) on *every* poll just to find an empty inbox — millions of wasted tokens/day. Prefer
the **token-free poller** `scripts/atmcp_worker_poller.py`: it long-polls the inbox over plain
HTTP (zero tokens while idle) and invokes `claude -p` only when a directive actually arrives:

```bash
python scripts/atmcp_worker_poller.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name bob --model opus   # --dry-run to test
```

By default it keeps **one resumable session per worker** (`--session-mode resume`: captures the
Claude `session_id` and `--resume`s it each directive) so the worker **remembers prior tasks**
while idle polling stays token-free; the inbox long-poll returns the instant a directive is sent
(don't use `/loop` — 1-min cron). It uses the worker REST API (`GET …/agents/{agent}/inbox`
long-poll, `POST …/directives/{id}/claim`, `…/report`). If you'd rather run the `atmcp-worker`
**skill** as an agent loop, use the runner
`scripts/atmcp_worker_runner.{sh,ps1}` (fast poller model + Opus `atmcp-executor` subagent) —
and avoid bare `/loop /atmcp-worker` (dynamic mode can silently stop, esp. Windows PowerShell;
use `/loop 30s`).

## Making agents actually use it

MCP is pull, not push: the tools are available, but the model decides when to call them and
has no timer. See **[`prompts/`](prompts/README.md)** for the per-client workflow rules, the
auto-join headers (`Authorization` + `X-ATMcp-Agent`), and three ways to keep presence fresh
(model-driven, the sidecar, or a client hook). The REST presence endpoint
`POST /api/teams/{team}/heartbeat` (auth = join token) backs the sidecar.
