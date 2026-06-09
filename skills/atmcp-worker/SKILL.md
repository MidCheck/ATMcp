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
4. **Execute its `instruction`** exactly as if the user had asked you directly. As you work,
   call `append_output(text="<short progress/result chunk>", directive_id="<id>")` so the
   console can watch you live. Also `heartbeat(status_summary="<what you're doing>", progress_pct=…)`.
5. When finished, `report_directive(directive_id, status="done", result_summary="<1–2 lines>",
   output="<the useful result>")`. If it failed, use `status="failed"` with the error in
   `result_summary`.
6. Stop (one cycle done).

## Running continuously

Keep yourself available by looping this skill, e.g. in Claude Code:

```
/loop /atmcp-worker
```

Your MCP client should be configured with the team headers
(`Authorization: Bearer <join_token>` + `X-ATMcp-Agent: <your-name>`) so you're auto-joined
and the console can address you by `<your-name>`. Run the presence sidecar too if you want a
rock-solid online indicator while you think.
