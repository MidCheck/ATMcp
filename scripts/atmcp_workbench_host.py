#!/usr/bin/env python3
"""ATMcp workbench worker host — concurrent, streaming, multi-session (Phase 1: claude).

The evolution of atmcp_worker_poller.py for the workbench: instead of one serial session
per worker, this host runs MULTIPLE conversation threads (sessions) for one agent
CONCURRENTLY, streams each one's output token-by-token back to the server in real time, and
isolates each session in its own git worktree. The old poller is left untouched (it still
serves the agent's default/legacy thread).

Per directive (a chat message into a thread):
  claim → ensure the session's worktree → run `claude -p --output-format stream-json
  --include-partial-messages` resuming THAT thread's claude session → stream text deltas to
  the server (tagged with session_id) → report done/failed → record usage + the thread's
  resumable session id.

Concurrency: different sessions run in parallel (asyncio); a single session is serial (a
conversation can't run two turns at once). Idle is token-free HTTP long-polling.

Pure stdlib + asyncio. Example:
  python scripts/atmcp_workbench_host.py --url http://localhost:8000 \
      --team my-team --token "<join_token>" --name bob --base-repo ~/code/myrepo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

EXEC_PROMPT = (
    "You are a member of an agent team, working in one conversation thread. You have the "
    "context of earlier messages in THIS thread. Carry out the message below, then finish with "
    "a short summary of what you did.\n\nMESSAGE:\n{instruction}"
)
DEFAULT_KEY = "__default__"   # lane key for directives with no session_id


# ── stream-json parsing (pure, unit-tested) ──────────────────────────────────
def classify_event(obj: dict) -> tuple[str, object]:
    """Map one parsed stream-json line to ('init', session_id) | ('delta', text) |
    ('assistant', text) | ('result', obj) | ('other', None)."""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        return ("init", obj.get("session_id"))
    if t == "stream_event":
        ev = obj.get("event") or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                return ("delta", delta.get("text") or "")
        return ("other", None)
    if t == "assistant":
        msg = obj.get("message") or {}
        text = "".join(
            b.get("text", "") for b in (msg.get("content") or []) if b.get("type") == "text"
        )
        return ("assistant", text)
    if t == "result":
        return ("result", obj)
    return ("other", None)


def extract_usage(result_obj: dict, model: str) -> dict | None:
    u = result_obj.get("usage") or {}
    usage = {
        "model": result_obj.get("model") or model,
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
        "cost_usd": float(result_obj.get("total_cost_usd") or 0.0),
        "num_turns": int(result_obj.get("num_turns") or 0),
        "duration_ms": int(result_obj.get("duration_ms") or 0),
    }
    return usage if any(usage[k] for k in ("input_tokens", "output_tokens", "cost_usd")) else None


def _win_wrap(cmd: list[str]) -> list[str]:
    """Windows: resolve a `claude.cmd` npm shim via PATHEXT and run it through cmd /c
    (CreateProcess can't launch .cmd directly). No-op off Windows / for a real .exe."""
    if os.name != "nt":
        return cmd
    exe = shutil.which(cmd[0])
    if not exe:
        return cmd
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def build_claude_cmd(args, cli_session_id: str | None) -> list[str]:
    cmd = [
        "claude", "-p", "--output-format", "stream-json", "--include-partial-messages",
        "--verbose", "--model", args.model,
    ]
    if args.allowed_tools:
        cmd += ["--allowedTools", args.allowed_tools]
    if args.permission_mode:
        cmd += ["--permission-mode", args.permission_mode]
    if cli_session_id:
        cmd += ["--resume", cli_session_id]
    extra = (args.claude_args or "")
    if extra:
        cmd += shlex.split(extra)
    return _win_wrap(cmd)


# CLI-text executors (codex/cursor): a command template with a {prompt} token. Per-session
# memory comes from running in the session's own worktree (cwd) + the tool's own resume flag —
# add it in the template, e.g. "codex exec resume --last {prompt}". These stream plain stdout
# (no structured token deltas / usage). Best-effort; override freely with --executor-cmd.
DEFAULT_TEMPLATES = {
    "codex": "codex exec {prompt}",
    "cursor": "cursor-agent -p {prompt}",
}


def resolve_template(args) -> str | None:
    """The CLI-text template to use, or None to use the structured claude driver."""
    if args.executor_cmd:
        return args.executor_cmd
    if args.executor and args.executor != "claude":
        return DEFAULT_TEMPLATES.get(args.executor, "{prompt}")
    return None


def build_custom_cmd(template: str, prompt: str) -> list[str]:
    """Split the template safely; substitute {prompt} as ONE argv element (no shell, no injection)."""
    parts = shlex.split(template)
    if "{prompt}" in parts:
        return [prompt if p == "{prompt}" else p for p in parts]
    return parts + [prompt]


# ── HTTP client (urllib, run off the event loop via to_thread) ────────────────
class Client:
    def __init__(self, url: str, team: str, token: str, name: str):
        self.base = url.rstrip("/")
        self.team = team
        self.token = token
        self.name = name

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    async def req(self, method: str, path: str, body: dict | None = None, timeout: float = 15.0) -> dict:
        return await asyncio.to_thread(self._req, method, path, body, timeout)

    async def heartbeat(self, status: str) -> None:
        try:
            await self.req("POST", f"/api/teams/{self.team}/heartbeat",
                           {"display_name": self.name, "status_summary": status})
        except Exception:
            pass

    async def inbox(self, wait_ms: int) -> list[dict]:
        try:
            r = await self.req("GET",
                               f"/api/teams/{self.team}/agents/{self.name}/inbox?wait_ms={wait_ms}",
                               timeout=wait_ms / 1000.0 + 10.0)
            return r.get("directives", [])
        except Exception:
            return []

    async def claim(self, did: str) -> bool:
        try:
            return bool((await self.req(
                "POST", f"/api/teams/{self.team}/directives/{did}/claim", {"agent": self.name}
            )).get("ok"))
        except Exception:
            return False

    async def report(self, did: str, status: str, summary: str, output: str) -> None:
        try:
            await self.req("POST", f"/api/teams/{self.team}/directives/{did}/report",
                           {"agent": self.name, "status": status, "result_summary": summary,
                            "output": output})
        except Exception:
            pass

    async def append_output(self, text: str, session_id: str | None, did: str | None) -> None:
        try:
            await self.req("POST", f"/api/teams/{self.team}/agents/{self.name}/output",
                           {"text": text, "directive_id": did, "session_id": session_id})
        except Exception:
            pass

    async def report_usage(self, usage: dict, session_id: str | None, did: str | None) -> None:
        try:
            await self.req("POST", f"/api/teams/{self.team}/agents/{self.name}/usage",
                           {**usage, "directive_id": did})
        except Exception:
            pass

    async def set_executor(self, session_id: str, cli_session_id: str | None, worktree: str | None) -> None:
        try:
            await self.req("POST", f"/api/teams/{self.team}/sessions/{session_id}/executor",
                           {"cli_session_id": cli_session_id, "worktree": worktree})
        except Exception:
            pass


# ── per-session local state (resume id map) + worktrees ──────────────────────
def _state_path(args) -> str:
    safe = f"{args.team}__{args.name}".replace("/", "_")
    return os.path.join(os.path.expanduser(args.state_dir), f"wb__{safe}.json")


def load_sid_map(args) -> dict:
    try:
        with open(_state_path(args), encoding="utf-8") as f:
            return (json.load(f) or {}).get("sessions", {})
    except Exception:
        return {}


def save_sid_map(args, sid_map: dict) -> None:
    path = _state_path(args)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"team": args.team, "name": args.name, "sessions": sid_map}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        print(f"[atmcp] warn: could not persist state: {e}", file=sys.stderr, flush=True)


def ensure_worktree(args, session_id: str) -> str | None:
    """Give the session its own working dir. If --base-repo is a git repo, add a worktree;
    else a per-session subdir. Returns the cwd to run in (or None to use the process cwd)."""
    if not args.base_repo:
        return None
    base = os.path.expanduser(args.base_repo)
    root = os.path.join(os.path.expanduser(args.state_dir), "worktrees", f"{args.team}__{args.name}")
    path = os.path.join(root, session_id)
    if os.path.isdir(path):
        return path
    os.makedirs(root, exist_ok=True)
    if os.path.isdir(os.path.join(base, ".git")):
        # Detached worktree (no branch) avoids branch-name collisions entirely; prune first
        # so a stale registration (dir deleted out from under git) doesn't block the add.
        try:
            subprocess.run(["git", "-C", base, "worktree", "prune"], capture_output=True, text=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            subprocess.run(["git", "-C", base, "worktree", "add", "--detach", path, "HEAD"],
                           check=True, capture_output=True, text=True)
            return path
        except subprocess.CalledProcessError as e:
            # NEVER fall back to the shared base repo (concurrent acceptEdits would clobber it);
            # use an isolated empty dir instead — degraded (no repo files) but safe.
            print(f"[atmcp] worktree add failed ({(e.stderr or '').strip()[:120]}); "
                  f"using isolated dir", file=sys.stderr, flush=True)
    os.makedirs(path, exist_ok=True)
    return path


# ── drivers ──────────────────────────────────────────────────────────────────
async def _drain(stream, sink: list[str]) -> None:
    try:
        async for line in stream:
            sink.append(line.decode(errors="replace"))
    except Exception:  # noqa: BLE001
        pass


async def _drive_claude(args, c: Client, session_id, did, cli_sid, cwd, prompt):
    """Structured stream-json driver: token deltas, captured session id, usage. Returns
    (ok, final_text, new_cli_sid, usage)."""
    cmd = build_claude_cmd(args, cli_sid)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, limit=8 * 1024 * 1024,
        )
    except FileNotFoundError:
        return False, f"executor not found: {cmd[0]}", None, None

    stderr_chunks: list[str] = []
    stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_chunks))
    try:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

    new_cli_sid = cli_sid
    result_obj: dict | None = None
    got_delta = False
    buf: list[str] = []
    buf_len = 0

    async def flush():
        nonlocal buf, buf_len
        if buf:
            await c.append_output("".join(buf), session_id, did)
            buf, buf_len = [], 0

    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind, val = classify_event(obj)
            if kind == "init" and val:
                new_cli_sid = val
            elif kind == "delta" and val:
                got_delta = True
                buf.append(val); buf_len += len(val)
                if buf_len >= 200:
                    await flush()
            elif kind == "assistant" and val and not got_delta:
                buf.append(val); buf_len += len(val); await flush()
            elif kind == "result":
                result_obj = obj
    except Exception as e:  # noqa: BLE001 — a stream read error must not wedge the lane
        stderr_chunks.append(f"[stream read error: {e}]")
    await flush()
    await proc.wait()
    await stderr_task
    stderr = "".join(stderr_chunks)

    ok = bool(result_obj) and not result_obj.get("is_error", False) and proc.returncode == 0
    final_text = (result_obj or {}).get("result") or ""
    if not got_delta and final_text:
        await c.append_output(final_text, session_id, did)
    if not ok and not final_text:
        final_text = (stderr.strip() or f"exit {proc.returncode}")[:4000]
    return ok, final_text, new_cli_sid, extract_usage(result_obj or {}, args.model)


async def _drive_cli_text(args, c: Client, session_id, did, cwd, prompt, template):
    """Generic CLI driver (codex/cursor/custom): runs the template in the session's worktree
    and streams plain stdout. No structured deltas / usage; resume is the tool's own (cwd +
    its --last/--resume flag in the template). Returns (ok, final_text, None, None)."""
    cmd = _win_wrap(build_custom_cmd(template, prompt))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, limit=8 * 1024 * 1024,
        )
    except FileNotFoundError:
        return False, f"executor not found: {cmd[0]}", None, None

    stderr_chunks: list[str] = []
    stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_chunks))
    tail: list[str] = []
    buf: list[str] = []
    buf_len = 0

    async def flush():
        nonlocal buf, buf_len
        if buf:
            await c.append_output("".join(buf), session_id, did)
            buf, buf_len = [], 0

    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            t = line.rstrip("\n")
            if t:
                tail.append(t)
                if len(tail) > 50:
                    del tail[:-50]
            buf.append(line); buf_len += len(line)
            if buf_len >= 200:
                await flush()
    except Exception as e:  # noqa: BLE001
        stderr_chunks.append(f"[stream read error: {e}]")
    await flush()
    await proc.wait()
    await stderr_task
    stderr = "".join(stderr_chunks)

    ok = proc.returncode == 0
    final_text = "\n".join(tail).strip()
    if not ok:
        final_text = ((final_text + "\n" + stderr).strip() or f"exit {proc.returncode}")[:4000]
    return ok, final_text[:8000], None, None


# ── one directive (one thread turn) ──────────────────────────────────────────
async def handle_directive(args, c: Client, d: dict, sid_map: dict, lock: asyncio.Lock) -> None:
    did = d["directive_id"]
    session_id = d.get("session_id")
    instruction = d["instruction"]
    key = session_id or DEFAULT_KEY
    prompt = EXEC_PROMPT.format(instruction=instruction)

    cwd = await asyncio.to_thread(ensure_worktree, args, session_id) if session_id else None
    cli_sid = sid_map.get(key)
    template = resolve_template(args)

    await c.heartbeat(f"thread {key[:6]}: {instruction[:32]}")
    await c.append_output("▶ " + instruction[:200], session_id, did)

    if args.dry_run:
        ex = template or ("claude --model " + args.model)
        await c.append_output(f"[dry-run] would run `{ex}` (resume={bool(cli_sid)}) in {cwd or 'cwd'}",
                              session_id, did)
        await c.report(did, "done", "[dry-run]", "[dry-run]")
        return

    if template is None:
        ok, final_text, new_cli_sid, usage = await _drive_claude(args, c, session_id, did, cli_sid, cwd, prompt)
    else:
        ok, final_text, new_cli_sid, usage = await _drive_cli_text(args, c, session_id, did, cwd, prompt, template)

    # persist the thread's resumable session id (claude only; under the lock — shared map)
    if new_cli_sid and new_cli_sid != cli_sid:
        async with lock:
            sid_map[key] = new_cli_sid
            await asyncio.to_thread(save_sid_map, args, sid_map)
        if session_id:
            await c.set_executor(session_id, new_cli_sid, cwd)
    if usage:
        await c.report_usage(usage, session_id, did)

    summary = (final_text.splitlines()[0] if final_text else "")[:200]
    await c.report(did, "done" if ok else "failed", summary, final_text[:8000])
    print(f"[atmcp] thread {key[:6]} {did[-6:]}: {'done' if ok else 'failed'}"
          f"{' · ' + str(usage['input_tokens'] + usage['output_tokens']) + 'tok' if usage else ''}",
          flush=True)


async def main_async(args) -> None:
    c = Client(args.url, args.team, args.token, args.name)
    sid_map = load_sid_map(args)
    lock = asyncio.Lock()
    running: dict[str, asyncio.Task] = {}
    print(f"[atmcp] workbench host '{args.name}' team='{args.team}' model={args.model} "
          f"max-concurrent={args.max_concurrent} base-repo={args.base_repo or '(none)'}"
          f"{' (dry-run)' if args.dry_run else ''}. Ctrl-C to stop.", flush=True)

    def _reap(t: asyncio.Task, k: str) -> None:
        # free the lane the instant the turn finishes (not on the next 30s poll)
        if running.get(k) is t:
            running.pop(k, None)
        if not t.cancelled() and t.exception():
            print(f"[atmcp] lane {k[:6]} error: {t.exception()}", file=sys.stderr, flush=True)

    while True:
        try:
            await c.heartbeat(f"hosting ({len(running)} active)")
            directives = await c.inbox(args.wait_ms)
            for d in directives:
                key = d.get("session_id") or DEFAULT_KEY
                if key in running:                       # that thread is busy → skip (next poll)
                    continue
                if len(running) >= args.max_concurrent:  # at capacity
                    break
                if not await c.claim(d["directive_id"]):
                    continue
                task = asyncio.create_task(handle_directive(args, c, d, sid_map, lock))
                running[key] = task
                task.add_done_callback(lambda t, k=key: _reap(t, k))
            if not directives:
                await asyncio.sleep(args.idle_sleep)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[atmcp] loop error: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(2)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="ATMcp workbench worker host (concurrent, streaming)")
    ap.add_argument("--url", default=os.environ.get("ATMCP_URL", "http://localhost:8000"))
    ap.add_argument("--team", default=os.environ.get("ATMCP_TEAM"), required=not os.environ.get("ATMCP_TEAM"))
    ap.add_argument("--token", default=os.environ.get("ATMCP_TOKEN"), required=not os.environ.get("ATMCP_TOKEN"))
    ap.add_argument("--name", default=os.environ.get("ATMCP_NAME"), required=not os.environ.get("ATMCP_NAME"))
    ap.add_argument("--model", default=os.environ.get("ATMCP_MODEL", "opus"))
    ap.add_argument("--executor", default=os.environ.get("ATMCP_EXECUTOR", "claude"),
                    choices=["claude", "codex", "cursor"],
                    help="executor bound to this agent. claude = structured streaming + usage; "
                         "codex/cursor = generic CLI streaming (per-session worktree + the tool's "
                         "own resume flag). Override the command with --executor-cmd.")
    ap.add_argument("--executor-cmd", default=os.environ.get("ATMCP_EXECUTOR_CMD"),
                    help="custom CLI template with a {prompt} token (wins over --executor), e.g. "
                         "\"codex exec resume --last {prompt}\" / \"cursor-agent -p --resume {prompt}\"")
    ap.add_argument("--base-repo", default=os.environ.get("ATMCP_BASE_REPO"),
                    help="git repo to base per-session worktrees on (omit = run in process cwd)")
    ap.add_argument("--state-dir", default=os.environ.get("ATMCP_STATE_DIR", "~/.atmcp"))
    ap.add_argument("--allowed-tools", default=os.environ.get("ATMCP_ALLOWED", "mcp__atmcp,Read,Edit,Bash,Write,Grep,Glob"))
    ap.add_argument("--permission-mode", default=os.environ.get("ATMCP_PERMISSION_MODE", "acceptEdits"))
    ap.add_argument("--claude-args", default=os.environ.get("ATMCP_CLAUDE_ARGS", ""))
    ap.add_argument("--max-concurrent", type=int, default=int(os.environ.get("ATMCP_MAX_CONCURRENT") or 4))
    ap.add_argument("--wait-ms", type=int, default=30000)
    ap.add_argument("--idle-sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[atmcp] stopped", flush=True)


if __name__ == "__main__":
    main()
