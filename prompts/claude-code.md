# ATMcp with Claude Code

## 1. Connect (with team headers → auto-join)

```bash
claude mcp add --transport http atmcp http://<host>:8000/mcp \
  --header "Authorization: Bearer <join_token>" \
  --header "X-ATMcp-Agent: <your-display-name>"
```

With these headers the first tool call auto-joins the team as `<your-display-name>`, so the
agent doesn't even need to call `join_team`. (You can also pass the token to `join_team`
directly if you prefer not to set headers.)

## 2. Tell the agent the workflow

Append the contents of [`agent-workflow.md`](agent-workflow.md) (中文版:
[`agent-workflow.zh-CN.md`](agent-workflow.zh-CN.md)) to your project's `CLAUDE.md`
(or pass it via `--append-system-prompt "$(cat prompts/agent-workflow.md)"`).

## 3. Keep presence fresh

**Option B — sidecar (recommended):** run alongside the session:

```bash
python scripts/atmcp_heartbeat.py --url http://<host>:8000 \
  --team <team> --token <join_token> --name <your-display-name> --interval 10
```

**Option C — hook:** fire a heartbeat after every tool use via a hook in `settings.json`.
Set `ATMCP_URL`, `ATMCP_TEAM`, `ATMCP_TOKEN`, `ATMCP_NAME` in your environment first.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST \"$ATMCP_URL/api/teams/$ATMCP_TEAM/heartbeat\" -H \"Authorization: Bearer $ATMCP_TOKEN\" -H 'Content-Type: application/json' -d \"{\\\"display_name\\\":\\\"$ATMCP_NAME\\\",\\\"status_summary\\\":\\\"working\\\"}\" >/dev/null 2>&1 || true"
          }
        ]
      }
    ]
  }
}
```

This keeps the agent "online" whenever it is actively using tools; the sidecar (Option B)
additionally covers the gaps while it is only thinking.
