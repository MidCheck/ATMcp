"""Workbench host pure helpers: stream-json parsing, usage extraction, claude cmd build,
and per-session worktree allocation (git + non-git + none)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "atmcp_workbench_host.py"))


def _load():
    spec = importlib.util.spec_from_file_location("atmcp_wb_host", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


host = _load()


# ── stream-json classification ───────────────────────────────────────────────
def test_classify_init_event():
    assert host.classify_event({"type": "system", "subtype": "init", "session_id": "s9"}) == ("init", "s9")


def test_classify_text_delta():
    ev = {"type": "stream_event",
          "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"}}}
    assert host.classify_event(ev) == ("delta", "hel")


def test_classify_non_text_delta_is_other():
    ev = {"type": "stream_event",
          "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}}
    assert host.classify_event(ev) == ("other", None)


def test_classify_assistant_whole_message():
    obj = {"type": "assistant",
           "message": {"content": [{"type": "text", "text": "done"}, {"type": "tool_use"}]}}
    assert host.classify_event(obj) == ("assistant", "done")


def test_classify_result_passthrough():
    kind, val = host.classify_event({"type": "result", "is_error": False, "result": "ok"})
    assert kind == "result" and val["result"] == "ok"


def test_classify_simulated_stream_accumulates():
    lines = [
        {"type": "system", "subtype": "init", "session_id": "sess-1"},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "result", "is_error": False, "result": "Hello", "session_id": "sess-1",
         "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 2}},
    ]
    sid, text, result = None, "", None
    for o in lines:
        k, v = host.classify_event(o)
        if k == "init":
            sid = v
        elif k == "delta":
            text += v
        elif k == "result":
            result = v
    assert sid == "sess-1" and text == "Hello" and result["result"] == "Hello"


# ── usage extraction ─────────────────────────────────────────────────────────
def test_extract_usage_from_result():
    r = {"total_cost_usd": 0.2, "model": "claude-opus-4-8",
         "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 9}}
    u = host.extract_usage(r, "opus")
    assert u["input_tokens"] == 100 and u["output_tokens"] == 40 and u["cost_usd"] == 0.2
    assert u["cache_read"] == 9 and u["model"] == "claude-opus-4-8"


def test_extract_usage_none_when_empty():
    assert host.extract_usage({}, "opus") is None


# ── claude command build ─────────────────────────────────────────────────────
def _args(**kw):
    base = dict(model="opus", allowed_tools="mcp__atmcp", permission_mode="acceptEdits", claude_args="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_cmd_is_streaming_and_resumes():
    cmd = host.build_claude_cmd(_args(), "sess-abc")
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--include-partial-messages" in cmd
    assert "--resume" in cmd and "sess-abc" in cmd
    assert "--permission-mode" in cmd and "acceptEdits" in cmd


def test_build_cmd_no_resume_when_fresh():
    cmd = host.build_claude_cmd(_args(), None)
    assert "--resume" not in cmd


def test_build_cmd_passes_extra_args():
    cmd = host.build_claude_cmd(_args(claude_args="--add-dir /x"), None)
    assert "--add-dir" in cmd and "/x" in cmd


# ── per-session worktree ─────────────────────────────────────────────────────
def test_worktree_none_when_no_base_repo(tmp_path):
    args = SimpleNamespace(base_repo=None, state_dir=str(tmp_path), team="t", name="bob")
    assert host.ensure_worktree(args, "sX") is None


def test_worktree_non_git_makes_separate_dir(tmp_path):
    base = tmp_path / "plain"
    base.mkdir()
    args = SimpleNamespace(base_repo=str(base), state_dir=str(tmp_path / "state"), team="t", name="bob")
    p = host.ensure_worktree(args, "sess-xyz")
    assert p and os.path.isdir(p) and p != str(base)


# ── Phase 2: codex/cursor CLI-text driver ────────────────────────────────────
def test_resolve_template():
    assert host.resolve_template(SimpleNamespace(executor="claude", executor_cmd=None)) is None
    assert host.resolve_template(SimpleNamespace(executor="codex", executor_cmd=None)) == host.DEFAULT_TEMPLATES["codex"]
    assert host.resolve_template(SimpleNamespace(executor="cursor", executor_cmd=None)) == host.DEFAULT_TEMPLATES["cursor"]
    # an explicit --executor-cmd wins over the named executor
    assert host.resolve_template(SimpleNamespace(executor="codex", executor_cmd="x {prompt}")) == "x {prompt}"


def test_custom_cmd_argv_safe_against_injection():
    evil = "a; rm -rf / #"
    cmd = host.build_custom_cmd("codex exec resume --last {prompt}", evil)
    assert cmd[:4] == ["codex", "exec", "resume", "--last"]
    assert cmd[-1] == evil and sum(1 for p in cmd if evil in p) == 1   # malicious text stays one argv elem


def test_custom_cmd_appends_prompt_without_token():
    cmd = host.build_custom_cmd("mytool run", "hi")
    assert cmd[:2] == ["mytool", "run"] and cmd[-1] == "hi"


async def test_drive_cli_text_streams_and_reports(tmp_path):
    fake = tmp_path / "faketool.py"
    fake.write_text("print('line one')\nprint('line two')\n")
    template = f"{sys.executable} {fake} {{prompt}}"

    class Stub:
        def __init__(self): self.out = []
        async def append_output(self, text, sid, did): self.out.append(text)

    c = Stub()
    ok, final, sid, usage = await host._drive_cli_text(None, c, "s1", "d1", None, "do it", template)
    assert ok and sid is None and usage is None                 # CLI driver: no session id / usage
    assert "line one" in "".join(c.out)                          # streamed to the thread
    assert "line two" in final                                   # tail used for the report


# ── Phase 3: OpenAI-compat driver (tools, guard, agent loop) ─────────────────
def test_safe_path(tmp_path):
    base = str(tmp_path)
    assert host._safe_path(base, "a/b.txt") == os.path.normpath(os.path.join(base, "a/b.txt"))
    assert host._safe_path(base, "/etc/passwd") is None      # absolute rejected
    assert host._safe_path(base, "../escape") is None         # parent escape rejected


def test_local_guard_deny():
    assert host.local_guard_deny("rm -rf /") is True
    assert host.local_guard_deny("sudo reboot") is True
    assert host.local_guard_deny("ls -la") is False


async def test_guard_allows_offline_fallback():
    class C:  # guard server unreachable
        async def guard_check(self, *a, **k): return None
    assert (await host.guard_allows(C(), "rm -rf /", "s"))[0] is False   # dangerous → denied offline
    assert (await host.guard_allows(C(), "ls", "s"))[0] is True          # safe → allowed offline


async def test_run_tool_bash_respects_guard(tmp_path):
    args = SimpleNamespace(tool_timeout=30)

    class Deny:
        async def guard_check(self, *a, **k): return {"decision": "deny", "reason": "nope"}
    out = await host._run_tool(args, Deny(), "s", str(tmp_path), "run_bash", {"command": "echo hi"})
    assert out.startswith("BLOCKED by guard")

    class Allow:
        async def guard_check(self, *a, **k): return {"decision": "allow"}
    out = await host._run_tool(args, Allow(), "s", str(tmp_path), "run_bash", {"command": "echo hi"})
    assert "hi" in out


async def test_run_tool_file_rw_and_escape(tmp_path):
    args = SimpleNamespace(tool_timeout=30)

    class C:
        async def guard_check(self, *a, **k): return {"decision": "allow"}
    c = C()
    assert "wrote" in await host._run_tool(args, c, "s", str(tmp_path), "write_file", {"path": "x.txt", "content": "hi"})
    assert await host._run_tool(args, c, "s", str(tmp_path), "read_file", {"path": "x.txt"}) == "hi"
    assert "BLOCKED" in await host._run_tool(args, c, "s", str(tmp_path), "read_file", {"path": "/etc/hosts"})


async def test_drive_openai_agent_loop(tmp_path, monkeypatch):
    # scripted endpoint: round 1 calls run_bash, round 2 gives the final answer
    responses = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "run_bash", "arguments": "{\"command\": \"echo hello\"}"}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"content": "All done."}}],
         "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
    ]
    it = iter(responses)
    monkeypatch.setattr(host, "_openai_chat", lambda args, msgs: next(it))

    class Stub:
        def __init__(self): self.out = []
        async def append_output(self, t, sid, did): self.out.append(t)
        async def guard_check(self, *a, **k): return {"decision": "allow"}

    c = Stub()
    args = SimpleNamespace(model="qwen", api_base="x", api_key="k", max_steps=8,
                           tool_timeout=30, openai_timeout=60, state_dir=str(tmp_path), team="t", name="bob")
    ok, final, sid, usage = await host._drive_openai(args, c, "s1", "d1", "s1", str(tmp_path), "list files", asyncio.Lock())
    assert ok and final == "All done." and sid is None
    assert usage["input_tokens"] == 22 and usage["output_tokens"] == 8     # summed across rounds
    assert any("hello" in o for o in c.out)                                 # the bash tool actually ran
    assert host.load_messages(args, "s1")                                   # memory persisted for the thread


def test_worktree_git_adds_real_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "a@b.c",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "a@b.c"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, env=env)
    args = SimpleNamespace(base_repo=str(repo), state_dir=str(tmp_path / "state"), team="t", name="bob")
    p = host.ensure_worktree(args, "sess1234abcd")
    assert p and os.path.isdir(p) and p != str(repo)
    assert os.path.exists(os.path.join(p, "f.txt"))   # it's a real checked-out worktree
