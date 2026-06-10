---
name: atmcp-worker
description: Act as an ATMcp team worker for one cycle — poll your directive inbox, execute the instruction, stream your output, and report the result. Use when the user types /atmcp-worker or /loop /atmcp-worker.
---

# atmcp-worker — one work cycle

You are a **worker** agent **cursor** on team **rc-superstars**, connected via ATMcp MCP (`atmcp` server in `.cursor/mcp.json`).

Perform exactly **ONE** cycle:

1. `inbox(wait_ms=25000)` — long-poll for a directive addressed to you.
2. If empty → `heartbeat(status_summary="idle", progress_pct=0)` and stop.
3. Else take the first directive → `claim_directive(directive_id)`.
4. Execute its `instruction` as if the user asked directly. While working:
   - `append_output(text="...", directive_id="...")`
   - `heartbeat(status_summary="...", progress_pct=...)`
5. Finish with `report_directive(directive_id, status="done", result_summary="...", output="...")` (or `failed`).
6. Stop (one cycle done).

## Continuous availability

In Claude Code: `/loop /atmcp-worker`. In Cursor: run this skill again when idle, or ask the user to re-invoke `/atmcp-worker` after each cycle.

Optional presence sidecar:

```bash
python scripts/atmcp_heartbeat.py --url http://127.0.0.1:18000 \
  --team rc-superstars --token <join_token> --name cursor --interval 10
```
