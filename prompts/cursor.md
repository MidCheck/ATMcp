# ATMcp with Cursor

## 1. Connect — `.cursor/mcp.json`

```json
{
  "mcpServers": {
    "atmcp": {
      "url": "http://<host>:8000/mcp",
      "headers": {
        "Authorization": "Bearer <join_token>",
        "X-ATMcp-Agent": "<your-display-name>"
      }
    }
  }
}
```

The headers auto-join the agent to the team on its first tool call.

## 2. Rules — `.cursor/rules/atmcp.mdc`

Create `.cursor/rules/atmcp.mdc` with this frontmatter, then paste the body of
[`agent-workflow.md`](agent-workflow.md) below it:

```mdc
---
description: Collaborate with the agent team via ATMcp tools
alwaysApply: true
---

<paste the contents of prompts/agent-workflow.md here>
```

## 3. Presence sidecar (recommended)

```bash
python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name <your-display-name> --interval 10
```
