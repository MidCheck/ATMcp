#!/usr/bin/env python3
"""ATMcp token-efficient worker: poll WITHOUT an LLM, spend tokens only on real work.

The expensive part of a worker loop is that every poll is a full model turn (system prompt +
~33 MCP tool schemas) just to discover "nothing to do". This script removes that: it
long-polls the directive inbox over plain HTTP (zero tokens), and only when a directive
arrives does it invoke `claude -p` (a strong model) to execute it, then reports the result.

Idle cost ≈ one HTTP request per ~30s. Model cost ≈ only when there's actual work.

Pure stdlib. Example:
    python scripts/atmcp_worker_poller.py \
        --url http://localhost:8000 --team my-team --token "<join_token>" \
        --name bob --model opus

Prereqs: `claude` CLI on PATH with the project's MCP server configured (so the executor can
use team tools if a directive needs them). Use --dry-run to test the loop without invoking the model.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self, url: str, team: str, token: str, name: str):
        self.base = url.rstrip("/")
        self.team = team
        self.token = token
        self.name = name

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def heartbeat(self, status: str) -> None:
        try:
            self._req("POST", f"/api/teams/{self.team}/heartbeat",
                      {"display_name": self.name, "status_summary": status})
        except Exception:
            pass

    def inbox(self, wait_ms: int) -> list[dict]:
        r = self._req("GET", f"/api/teams/{self.team}/agents/{self.name}/inbox?wait_ms={wait_ms}",
                      timeout=wait_ms / 1000.0 + 10.0)
        return r.get("directives", [])

    def claim(self, did: str) -> dict:
        return self._req("POST", f"/api/teams/{self.team}/directives/{did}/claim", {"agent": self.name})

    def report(self, did: str, status: str, summary: str, output: str) -> dict:
        return self._req("POST", f"/api/teams/{self.team}/directives/{did}/report",
                         {"agent": self.name, "status": status, "result_summary": summary, "output": output})

    def append_output(self, text: str, did: str | None = None) -> None:
        try:
            self._req("POST", f"/api/teams/{self.team}/agents/{self.name}/output",
                      {"text": text, "directive_id": did})
        except Exception:
            pass


def run_executor(model: str, allowed: str, instruction: str, timeout: float, dry: bool) -> tuple[bool, str]:
    prompt = (
        "You are executing a directive for your agent team. Carry it out, then finish with a "
        "concise summary of what you did and the key result.\n\nDIRECTIVE:\n" + instruction
    )
    if dry:
        return True, f"[dry-run] would execute with {model}: {instruction[:120]}"
    cmd = ["claude", "-p", "--model", model, "--allowedTools", allowed, prompt]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "executor timed out"
    except FileNotFoundError:
        return False, "`claude` CLI not found on PATH"
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return False, (out + "\n" + (p.stderr or "")).strip()[:8000] or f"exit {p.returncode}"
    return True, out[:8000]


def main() -> None:
    ap = argparse.ArgumentParser(description="ATMcp token-efficient worker poller")
    ap.add_argument("--url", default=os.environ.get("ATMCP_URL", "http://localhost:8000"))
    ap.add_argument("--team", default=os.environ.get("ATMCP_TEAM"), required=not os.environ.get("ATMCP_TEAM"))
    ap.add_argument("--token", default=os.environ.get("ATMCP_TOKEN"), required=not os.environ.get("ATMCP_TOKEN"))
    ap.add_argument("--name", default=os.environ.get("ATMCP_NAME"), required=not os.environ.get("ATMCP_NAME"))
    ap.add_argument("--model", default=os.environ.get("ATMCP_MODEL", "opus"), help="executor model")
    ap.add_argument("--allowed-tools", default=os.environ.get("ATMCP_ALLOWED", "mcp__atmcp,Read,Edit,Bash,Write,Grep,Glob"))
    ap.add_argument("--wait-ms", type=int, default=30000, help="inbox long-poll window")
    ap.add_argument("--idle-sleep", type=float, default=1.0, help="sleep after an empty poll")
    ap.add_argument("--executor-timeout", type=float, default=1800.0)
    ap.add_argument("--dry-run", action="store_true", help="don't invoke the model; echo instead")
    args = ap.parse_args()

    c = Client(args.url, args.team, args.token, args.name)
    print(f"[atmcp] poller '{args.name}' on team '{args.team}' — executor model={args.model}"
          f"{' (dry-run)' if args.dry_run else ''}. Idle polling is token-free. Ctrl-C to stop.", flush=True)

    while True:
        try:
            c.heartbeat("polling")
            directives = c.inbox(args.wait_ms)
            if not directives:
                time.sleep(args.idle_sleep)
                continue
            d = directives[0]
            did, instruction = d["directive_id"], d["instruction"]
            claim = c.claim(did)
            if not claim.get("ok"):
                continue  # someone else got it / not claimable
            print(f"[atmcp] executing {did[-6:]}: {instruction[:80]}", flush=True)
            c.heartbeat(f"executing: {instruction[:40]}")
            c.append_output(f"▶ started: {instruction[:200]}", did)
            ok, result = run_executor(args.model, args.allowed_tools, instruction,
                                      args.executor_timeout, args.dry_run)
            c.append_output(result, did)
            summary = (result.splitlines()[0] if result else "")[:200]
            c.report(did, "done" if ok else "failed", summary, result)
            print(f"[atmcp] reported {did[-6:]}: {'done' if ok else 'failed'}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[atmcp] HTTP {e.code}: {e.read().decode(errors='replace')[:200]}", file=sys.stderr, flush=True)
            time.sleep(3)
        except urllib.error.URLError as e:
            print(f"[atmcp] server unreachable ({e}); retrying", file=sys.stderr, flush=True)
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[atmcp] stopped", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[atmcp] loop error: {e}", file=sys.stderr, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
