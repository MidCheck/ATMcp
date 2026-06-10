#!/usr/bin/env python3
"""ATMcp presence sidecar — keep an agent shown 'online' on the dashboard without the
LLM having to call the heartbeat tool. Run it next to an agent for the duration of its work.

Pure stdlib (no deps). Example:

    python scripts/atmcp_heartbeat.py \
        --url http://localhost:8000 --team my-team \
        --token "<join_token>" --name alice --interval 10 --status "working"

Use the SAME --name as the agent's display_name (the X-ATMcp-Agent header / join_team
display_name) so the heartbeat maps to the same roster entry.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def beat(url: str, team: str, token: str, name: str, status: str | None) -> dict:
    payload = json.dumps({"display_name": name, "status_summary": status}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/teams/{team}/heartbeat",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main() -> None:
    ap = argparse.ArgumentParser(description="ATMcp presence sidecar")
    ap.add_argument("--url", default="http://localhost:8000", help="ATMcp base URL")
    ap.add_argument("--team", required=True, help="team name")
    ap.add_argument("--token", required=True, help="team join token")
    ap.add_argument("--name", required=True, help="agent display_name (must match the agent)")
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between heartbeats")
    ap.add_argument("--status", default="online", help="status_summary to report")
    ap.add_argument("--once", action="store_true", help="send one heartbeat and exit")
    args = ap.parse_args()

    def tick() -> None:
        try:
            r = beat(args.url, args.team, args.token, args.name, args.status)
            print(f"[atmcp] heartbeat ok: agent={r.get('agent_id', '?')[-6:]}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[atmcp] heartbeat HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[atmcp] heartbeat error: {e}", file=sys.stderr, flush=True)

    if args.once:
        tick()
        return
    print(f"[atmcp] heartbeating '{args.name}' -> team '{args.team}' every {args.interval}s "
          f"(Ctrl-C to stop)", flush=True)
    try:
        while True:
            tick()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[atmcp] stopped", flush=True)


if __name__ == "__main__":
    main()
