# Multi-Agent Workbench — design blueprint

**Status:** design draft, decisions locked (2026-06-20). No code yet.

A new, standalone, server-hosted page that turns ATMcp from a *command console* into a
*chat-style control plane* for a team of agents — usable from any device, anywhere, just by
opening a URL. It is **additive**: the dashboard, the directive bus, tasks, and the `/team`
command vocabulary all stay and stay compatible.

## Goal
Replace "you must sit in a dedicated agent CLI to command the team" with "open a web page and keep
directing the team". Left = a folder tree (team → agent → session); right = a web chat (streaming
output above, a pinned input box below). One window of continuous conversation == one session.

## Locked decisions
- **Concurrency:** multiple sessions per agent run **concurrently**.
- **Output:** **true incremental streaming** (not just started→final).
- **Tree items:** a tree leaf is a **session (conversation thread)**; it also surfaces that
  session's current task/status inline.
- **Executors:** `claude` / `codex` / `cursor` / OpenAI-compatible (Ollama, LM Studio, vLLM, hosted),
  **bound per agent** (an agent = one worker host + one executor).
- **Local models also act** (tool-calling): the OpenAI-compatible driver runs a real agent loop
  with tools, not just chat.
- **Isolation:** each session gets its own **git worktree**.

## Concept model
- **Team** — existing tenant.
- **Agent** — a worker identity bound to one executor (e.g. `bob`=claude, `ollama1`=llama3,
  `codex1`=codex). Appears in the tree when its worker host registers (heartbeat).
- **Session (thread)** — NEW first-class entity: one conversation thread = one independent memory.
  Maps to one underlying model session (a `claude`/`codex` session id for CLI drivers, or a
  server-stored transcript for API drivers). Naming note: ATMcp already uses "session" for the MCP
  transport id and the CLI session id; the UI concept is internally a **thread** that *references*
  an executor session.

## Architecture
```
[browser  /workbench]  ── HTTP + WebSocket ──┐
                                             ▼
[ATMcp server: sessions · directives · agent_output · hub fan-out]
                                             ▲  inbox long-poll + streamed deltas back
[Worker Host (evolution of the poller): concurrent · multi-driver · per-session worktree]
        ├─ ClaudeDriver / CodexDriver / CursorDriver → subprocess (stream-json / each format)
        └─ OpenAICompatDriver (Ollama, …)            → HTTP + SSE + its own agent loop
```

## Components

### 1. Worker Host (largest piece)
Today's poller is sync, single-session, ~40 lines. It evolves into an **async daemon**: one host per
agent, bound to one executor, running **N concurrent sessions**. Per session it: allocates a
worktree, binds a driver, persists state, meters tokens (reuses the problem-1 usage meter), and
**streams output deltas (tagged with session_id) back to the server**. Idle is still token-free
inbox long-polling.

### 2. Drivers
| Shape | Examples | Memory owner | Tools (Bash/Edit/MCP) | Streaming | Resume |
|---|---|---|---|---|---|
| **Agentic CLI** (already an agent) | `claude -p`, `codex exec`, `cursor-agent` | the CLI's session, on the **worker machine** (local) | built-in | claude `--output-format stream-json`; codex/cursor per-tool | CLI session id |
| **Raw LLM API** (stateless completion) | Ollama / any OpenAI-compatible endpoint | **server-stored transcript** | **none by default** — needs our agent loop | native SSE | replay transcript |

Driver interface ≈ `start(thread, user_msg, on_delta) -> (result, usage)`.

### 3. OpenAI-compatible agent loop (how local models "act")
- Give the model a system prompt + **tool function schemas** (Bash/Read/Edit/Write/Grep/Glob/`mcp__atmcp`).
- Loop: model → `tool_calls` → execute **inside the session's worktree** → feed results back → until final.
- **Every tool call goes through Command Guard** (see [`guard` — separate design]): a local model
  running Bash is the highest-risk surface, so the guard and this loop are co-dependent.
- Models without native function-calling → fall back to ReAct-style prompting, or restrict to
  tool-capable models.
- The **transcript is stored server-side** → cross-device / cross-worker resumable (this is the
  problem-2 "ATMcp as shared brain" realized for free on the API shape).

### 4. Per-session worktree
On the first file-touching action a session does `git worktree add` off the agent's configured base
repo (lazy; read-only/chat sessions need none); archived sessions reclaim it; non-git dirs use a
separate copied directory. CLI drivers use it as cwd; the API agent loop executes tools in it. This
is what makes concurrent file edits safe.

### 5. Server data model (additive migration)
- `agents` += `executor` + config (endpoint / model / key / base-repo).
- new `sessions` table (thread): agent, driver, `cli_session_id | transcript pointer`, worktree,
  title, status, timestamps.
- `directives` and `agent_output` += `session_id` (nullable).
- `events` += session lifecycle kinds.

### 6. Streaming path
The worker frequently POSTs (or WS-pushes) output deltas tagged with `session_id`; the server hub
fans them out to the workbench WebSocket. Small deltas are batched to avoid chattiness.

### 7. Workbench page `/workbench`
- **Left (collapsible) tree:** team → agent (executor badge + presence dot) → session (status /
  last-activity, Top-N + "show more"); a `+ new session` affordance per agent; a hamburger collapses
  the whole sidebar (mobile = full-screen chat).
- **Right (web chat):** breadcrumb (team / agent / session) + presence; an upward-scrolling
  transcript (streaming deltas, newest at bottom, auto-scroll, scroll up for history); a **pinned
  bottom input** (a message = a directive scoped to this session; also accepts `/status`, `/logs`…
  slash commands).
- Server-hosted, responsive, resumes on any device.

## Backward compatibility (important)
The redesign is **additive, not a teardown**. The directive bus, tasks, and the dashboard
(board/activity/knowledge/Tokens) all remain.

- **`/team` commands stay compatible.** `/team` is a thin command layer (`services/console.py`) over
  the directive bus + task tools, which are unchanged except for an **optional `session_id`**:
  - `status / send / dispatch / watch / logs / directives / cancel` all keep working.
  - A directive with **no `session_id` routes to the agent's default session** — identical to today's
    single-session behavior.
  - Both surfaces keep working: slash commands inside the workbench input, AND the Claude Code
    `team` skill (same server APIs).
  - Semantic mappings to know: `send bob "x"` → bob's default session (pick a specific thread in the
    workbench to target it); `logs bob` → all of bob's output by default (or a chosen session);
    `dispatch "…"` → still a claimable, session-less task.
- **Old pollers keep working.** The current poller ignores `session_id` and keeps serving the default
  session; multi-session/concurrency needs the new worker host. Both coexist during transition.
- **Migration is additive, no data loss.** `CREATE TABLE sessions IF NOT EXISTS` + `ALTER TABLE …
  ADD COLUMN session_id`; existing rows get `session_id = NULL` (default session). Applied on startup.

## Relationship to other designed work
- **Command Guard** (separate design) becomes a **dependency** of the API-driver agent loop — local
  models acting must pass the guard. Best advanced alongside this.
- **Problem 2 (poller shared memory)** is realized for free on the API driver via the server-stored
  transcript.
- **Problem 1 (usage meter + budget brake) and the transient-retry** are reused per session.

## Phased plan (each phase shippable)
1. **Phase 1:** sessions model + workbench page + **ClaudeDriver: concurrent + true streaming
   (stream-json) + per-session worktree**, end to end. Proves the whole skeleton.
2. **Phase 2:** codex / cursor drivers.
3. **Phase 3 (largest):** OpenAI-compatible driver + **agent loop + tools + Guard** (local models act).
4. **Phase 4:** session rename/archive, team-level dispatch sessions, mobile polish, streaming tuning.

## Config inputs needed before building
- **worktree base:** which repo/dir each agent maps to.
- **endpoints:** Ollama URL (e.g. `http://localhost:11434/v1`) + model names; where hosted-API keys live.
- Confirm **Phase 1 = claude only** to land the skeleton.

## Open items
- Concurrency caps per agent (resource limits) and queueing policy when over cap.
- Worktree reclamation policy (on archive vs idle TTL) and disk budgeting.
- Whether the dashboard console column is kept or deprecated once the workbench ships.
- Creating a *new agent* from the page = provisioning a worker process (out of scope here).
