---
name: atmcp-worker
description: Act as an ATMcp team worker for one cycle — poll your directive inbox, execute the instruction, stream your output, and report the result. Run under /loop to stay continuously available to the console.
---

# atmcp-worker — one work cycle

You are a **worker** in an agent team connected via ATMcp. The console agent sends you
directives; your job is to pick them up and execute them. Perform exactly ONE cycle:

1. `inbox(wait_ms=25000)` — long-poll for a directive addressed to you.
2. If the inbox is empty, briefly `heartbeat(status_summary="idle")` and stop (the `/loop`
   will run this skill again).
3. Take the first (highest-priority) directive and `claim_directive(directive_id)`.
4. **Execute its `instruction`.** Delegate the real work to the `atmcp-executor` subagent via
   the Task tool — that subagent runs on a stronger model (see `agents/atmcp-executor.md`),
   so this poller can stay on a cheap/fast model. Pass it the instruction (and any context).
   As work proceeds, `append_output(text="<short progress/result chunk>", directive_id="<id>")`
   so the console can watch live, and `heartbeat(status_summary="…", progress_pct=…)`.
   (If no executor subagent is configured, just do the work yourself.)
5. When finished, `report_directive(directive_id, status="done", result_summary="<1–2 lines>",
   output="<the executor's result>")`. On failure use `status="failed"` with the error.
6. Stop (one cycle done).

## Running continuously (read this)

Keep yourself available by looping this skill. **Most reliable (recommended):** an OS-level
runner that re-invokes Claude Code headless each tick — see `scripts/atmcp_worker_runner.sh`
(macOS/Linux) or `scripts/atmcp_worker_runner.ps1` (Windows). It runs the poller on a fast
model and survives any single turn ending or crashing.

In-app alternative: `/loop 30s /atmcp-worker` — **use an explicit interval**. Avoid bare
`/loop /atmcp-worker` (dynamic self-paced mode): it relies on the model re-arming a wakeup
every turn and can silently stop after a while (especially on Windows PowerShell).

Your MCP client should be configured with the team headers
(`Authorization: Bearer <join_token>` + `X-ATMcp-Agent: <your-name>`) so you're auto-joined
and the console can address you by `<your-name>`. Run the presence sidecar
(`scripts/atmcp_heartbeat.py`) too for a rock-solid online indicator while you think.
