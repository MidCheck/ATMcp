# Making agents actually use ATMcp

Adding the MCP server only makes the tools *available*. Agents (Claude Code, Cursor, Qwen,
…) will **not** auto-report — MCP is pull, not push, and LLMs have no internal timer. So:

1. **Configure the connection with team headers** (so the agent auto-joins on first use):
   - `Authorization: Bearer <join_token>` — required; maps to exactly one team.
   - `X-ATMcp-Agent: <display_name>` — recommended; a stable name across reconnects.
     (Without it a per-session name like `agent-1a2b3c4d` is generated, creating a new
     roster entry each reconnect.)
2. **Give the agent the workflow** — paste [`agent-workflow.md`](agent-workflow.md) into the
   agent's system prompt / rules (client-specific files below).
3. **For reliable presence**, run the sidecar so the agent stays "online" even while it's
   thinking and not calling tools:
   ```bash
   python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
       --team <team> --token <join_token> --name <display_name> --interval 10
   ```

## Per-client setup

| Client | Connect | Rules file |
|---|---|---|
| Claude Code | `claude mcp add --transport http ...` (see [claude-code.md](claude-code.md)) | `CLAUDE.md` |
| Cursor | `.cursor/mcp.json` (see [cursor.md](cursor.md)) | `.cursor/rules/atmcp.mdc` |
| Qwen / other | MCP config + system prompt (see [qwen.md](qwen.md)) | system prompt |

## Three ways to keep presence fresh (pick one)

- **A — let the model do it:** rely on step 2's prompt (`heartbeat` at each step). Zero
  infra; presence is coarse (an agent thinking for minutes may flip to "offline").
- **B — sidecar (recommended):** run `scripts/atmcp_heartbeat.py` per agent. Decoupled from
  the LLM, presence is stable. Authenticates with the join token, no MCP session needed.
- **C — client hook:** fire a heartbeat from a client event hook (e.g. Claude Code
  `PostToolUse`) — see [claude-code.md](claude-code.md).
