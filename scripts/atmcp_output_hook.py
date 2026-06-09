#!/usr/bin/env python3
"""ATMcp output-capture hook (Claude Code).

Reads a hook event from stdin (JSON with a `transcript_path`), extracts the most recent
assistant text from the transcript, and POSTs it to ATMcp so the console can `get_agent_output`
on this agent. Best-effort and silent on failure — never blocks the agent.

Wire it in Claude Code settings.json (Stop / PostToolUse), with these env vars set:
    ATMCP_URL   (default http://localhost:8000)
    ATMCP_TEAM  (team name)
    ATMCP_TOKEN (team join token)
    ATMCP_NAME  (this agent's display_name — must match X-ATMcp-Agent / join_team)

Example hook:
{
  "hooks": {
    "Stop": [{"matcher":"*","hooks":[{"type":"command",
      "command":"python /path/to/scripts/atmcp_output_hook.py"}]}]
  }
}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def _latest_assistant_text(transcript_path: str) -> str | None:
    try:
        lines = open(transcript_path, encoding="utf-8").read().splitlines()
    except Exception:
        return None
    for line in reversed(lines[-80:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") or rec
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
        else:
            text = ""
        if text:
            return text
    return None


def main() -> None:
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except Exception:
        hook = {}
    team = os.environ.get("ATMCP_TEAM")
    token = os.environ.get("ATMCP_TOKEN")
    name = os.environ.get("ATMCP_NAME")
    if not (team and token and name):
        return
    transcript = hook.get("transcript_path")
    text = _latest_assistant_text(transcript) if transcript else None
    if not text:
        return

    url = os.environ.get("ATMCP_URL", "http://localhost:8000").rstrip("/")
    body = json.dumps({"text": text[:8000]}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/api/teams/{team}/agents/{name}/output",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # never disrupt the agent


if __name__ == "__main__":
    main()
