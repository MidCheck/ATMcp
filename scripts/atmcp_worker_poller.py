#!/usr/bin/env python3
"""ATMcp token-efficient worker WITH session memory.

The expensive part of a worker loop is that every poll is a full model turn (system prompt +
~33 MCP tool schemas) just to discover "nothing to do". This script removes that: it
long-polls the directive inbox over plain HTTP (zero tokens), and only when a directive
arrives does it invoke the model to execute it — **resuming the SAME session each time** so
the worker keeps context/memory across directives (the model applies its own compaction).

  idle cost     ≈ one HTTP request per ~30s  (no model tokens)
  notice latency≈ milliseconds (the inbox long-poll returns the instant a directive is sent)
  memory        ≈ retained across directives via `claude -p --resume <session_id>`

Executors:
  * claude (default): captures `.session_id` from `--output-format json` and `--resume`s it.
    Session id is persisted per worker under --state-dir so it survives poller restarts.
  * custom (--executor-cmd "codex exec --last {prompt}" / "cursor-agent -p --resume {prompt}"):
    runs the template (argv-safe, no shell) in a per-worker --workdir; session continuity is
    handled by that tool's own resume/continue flag. (Cursor headless resume is unreliable.)

Pure stdlib. Examples:
  python scripts/atmcp_worker_poller.py --url http://localhost:8000 \
      --team my-team --token "<join_token>" --name bob --model opus
  python scripts/atmcp_worker_poller.py ... --executor-cmd "codex exec --last {prompt}"
Use --dry-run to test the loop without invoking the model.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

EXEC_PROMPT = (
    "A new directive has been assigned to you by your agent team. You have the context of your "
    "previous directives in this session. Carry this one out, then finish with a concise summary "
    "of what you did and the key result.\n\nDIRECTIVE:\n{instruction}"
)


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


# ── session state (per worker) ───────────────────────────────────────────────
def _state_path(args) -> str:
    safe = f"{args.team}__{args.name}".replace("/", "_")
    return os.path.join(os.path.expanduser(args.state_dir), f"{safe}.json")


def load_session_id(args) -> str | None:
    if args.session_mode != "resume":
        return None
    try:
        with open(_state_path(args), encoding="utf-8") as f:
            return json.load(f).get("session_id")
    except Exception:
        return None


def save_session_id(args, session_id: str | None) -> None:
    if not session_id or args.session_mode != "resume":
        return
    path = _state_path(args)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "team": args.team, "name": args.name}, f)
    except Exception as e:  # noqa: BLE001
        print(f"[atmcp] warn: could not persist session id: {e}", file=sys.stderr, flush=True)


# ── command builders (argv lists — never shell strings, so no injection) ──────
def build_claude_cmd(args, instruction: str, session_id: str | None) -> list[str]:
    cmd = ["claude", "-p", "--output-format", "json", "--model", args.model]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    if args.session_mode == "resume" and session_id:
        cmd += ["--resume", session_id]
    cmd.append(EXEC_PROMPT.format(instruction=instruction))
    return cmd


def build_custom_cmd(template: str, instruction: str) -> list[str]:
    # Split the template safely, then substitute the {prompt} token as ONE argv element.
    prompt = EXEC_PROMPT.format(instruction=instruction)
    parts = shlex.split(template)
    if "{prompt}" in parts:
        return [prompt if p == "{prompt}" else p for p in parts]
    return parts + [prompt]


def run_executor(args, instruction: str, session_id: str | None) -> tuple[bool, str, str | None]:
    """Returns (ok, result_text, new_session_id)."""
    if args.dry_run:
        mode = f"resume {session_id[-6:]}" if session_id else "new session"
        return True, f"[dry-run] would execute ({mode}) via {args.executor_cmd or 'claude'}: {instruction[:120]}", session_id

    cwd = None
    if args.executor_cmd:
        cmd = build_custom_cmd(args.executor_cmd, instruction)
        if args.workdir:
            cwd = os.path.join(os.path.expanduser(args.workdir), args.name)
            os.makedirs(cwd, exist_ok=True)
    else:
        cmd = build_claude_cmd(args, instruction, session_id)

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=args.executor_timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return False, "executor timed out", session_id
    except FileNotFoundError:
        return False, f"executor not found: {cmd[0]}", session_id

    out = (p.stdout or "").strip()
    # Claude --output-format json: parse {result, session_id, is_error}.
    if not args.executor_cmd:
        try:
            j = json.loads(out)
            result = (j.get("result") or "").strip()
            new_sid = j.get("session_id") or session_id
            ok = (p.returncode == 0) and not j.get("is_error", False)
            return ok, (result or out)[:8000], new_sid
        except Exception:
            pass  # not JSON — fall through to raw handling
    if p.returncode != 0:
        return False, (out + "\n" + (p.stderr or "")).strip()[:8000] or f"exit {p.returncode}", session_id
    return True, out[:8000], session_id


def main() -> None:
    ap = argparse.ArgumentParser(description="ATMcp token-efficient worker (with session memory)")
    ap.add_argument("--url", default=os.environ.get("ATMCP_URL", "http://localhost:8000"))
    ap.add_argument("--team", default=os.environ.get("ATMCP_TEAM"), required=not os.environ.get("ATMCP_TEAM"))
    ap.add_argument("--token", default=os.environ.get("ATMCP_TOKEN"), required=not os.environ.get("ATMCP_TOKEN"))
    ap.add_argument("--name", default=os.environ.get("ATMCP_NAME"), required=not os.environ.get("ATMCP_NAME"))
    ap.add_argument("--model", default=os.environ.get("ATMCP_MODEL", "opus"), help="executor model (claude executor)")
    ap.add_argument("--session-mode", choices=["resume", "fresh"], default="resume",
                    help="resume = keep one session per worker (memory); fresh = new session each task")
    ap.add_argument("--executor-cmd", default=os.environ.get("ATMCP_EXECUTOR_CMD"),
                    help="custom executor template with a {prompt} token, e.g. 'codex exec --last {prompt}'")
    ap.add_argument("--workdir", default=os.environ.get("ATMCP_WORKDIR"),
                    help="base dir; custom executor runs in <workdir>/<name> (for tool cwd-based resume)")
    ap.add_argument("--state-dir", default=os.environ.get("ATMCP_STATE_DIR", "~/.atmcp"),
                    help="where per-worker session ids are persisted")
    ap.add_argument("--allowed-tools", default=os.environ.get("ATMCP_ALLOWED", "mcp__atmcp,Read,Edit,Bash,Write,Grep,Glob"))
    ap.add_argument("--wait-ms", type=int, default=30000, help="inbox long-poll window")
    ap.add_argument("--idle-sleep", type=float, default=1.0)
    ap.add_argument("--executor-timeout", type=float, default=1800.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = Client(args.url, args.team, args.token, args.name)
    session_id = load_session_id(args)
    print(f"[atmcp] poller '{args.name}' team='{args.team}' executor={args.executor_cmd or ('claude --model ' + args.model)} "
          f"session-mode={args.session_mode}{' (resuming ' + session_id[-6:] + ')' if session_id else ''}"
          f"{' (dry-run)' if args.dry_run else ''}. Idle polling is token-free. Ctrl-C to stop.", flush=True)

    while True:
        try:
            c.heartbeat("polling")
            directives = c.inbox(args.wait_ms)
            if not directives:
                time.sleep(args.idle_sleep)
                continue
            d = directives[0]  # serial: one directive at a time
            did, instruction = d["directive_id"], d["instruction"]
            if not c.claim(did).get("ok"):
                continue
            print(f"[atmcp] executing {did[-6:]}: {instruction[:80]}", flush=True)
            c.heartbeat(f"executing: {instruction[:40]}")
            c.append_output(f"▶ started: {instruction[:200]}", did)

            ok, result, new_sid = run_executor(args, instruction, session_id)
            if new_sid and new_sid != session_id:
                session_id = new_sid
                save_session_id(args, session_id)

            c.append_output(result, did)
            summary = (result.splitlines()[0] if result else "")[:200]
            c.report(did, "done" if ok else "failed", summary, result)
            print(f"[atmcp] reported {did[-6:]}: {'done' if ok else 'failed'}"
                  f"{' · session ' + session_id[-6:] if session_id else ''}", flush=True)
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
