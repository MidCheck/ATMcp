# ATMcp with Qwen (qwen-code / qwen-agent / any MCP client)

ATMcp is a standard **streamable-HTTP MCP server**, so any MCP-capable runtime can use it.
Headers carry the team join token (and an optional stable agent name for auto-join).

## 1. Connect — MCP server config

Most Qwen/MCP runtimes accept an HTTP MCP server entry like:

```json
{
  "mcpServers": {
    "atmcp": {
      "type": "streamable-http",
      "url": "http://<host>:8000/mcp",
      "headers": {
        "Authorization": "Bearer <join_token>",
        "X-ATMcp-Agent": "<your-display-name>"
      }
    }
  }
}
```

(For `qwen-agent`, register the MCP server in the tool/MCP config the same way; the exact
key names follow your runtime's MCP schema. The important part is the URL + the two headers.)

If your runtime cannot send custom headers, pass the token explicitly from the agent instead:
`join_team(team_name="<TEAM>", display_name="<NAME>", join_token="<join_token>")`.

## 2. System prompt

Prepend the contents of [`agent-workflow.md`](agent-workflow.md) (中文版:
[`agent-workflow.zh-CN.md`](agent-workflow.zh-CN.md)) to the model's system prompt.

## 3. Presence sidecar (recommended)

```bash
python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name <your-display-name> --interval 10
```
