"""Poller command builders: resume wiring, pass-through flags, argv-safety.

The prompt is fed to `claude -p` via STDIN (see run_executor), so it is intentionally NOT
present in the argv built by build_claude_cmd — that keeps `claude -p` from ever mis-parsing
the prompt regardless of flag order.
"""

from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "atmcp_worker_poller.py"))


def _load():
    spec = importlib.util.spec_from_file_location("atmcp_poller", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


poller = _load()


def test_claude_cmd_resumes_session():
    args = SimpleNamespace(model="opus", allowed_tools="mcp__atmcp", session_mode="resume")
    cmd = poller.build_claude_cmd(args, "sess-123")
    assert cmd[:2] == ["claude", "-p"]
    assert "--resume" in cmd and "sess-123" in cmd
    assert "--output-format" in cmd and "json" in cmd
    # the prompt is fed via stdin, so it must NOT be in argv
    assert not any("DIRECTIVE" in str(x) for x in cmd)


def test_claude_cmd_no_resume_when_fresh_or_no_sid():
    fresh = poller.build_claude_cmd(SimpleNamespace(model="opus", allowed_tools="", session_mode="fresh"), "s1")
    assert "--resume" not in fresh
    nosid = poller.build_claude_cmd(SimpleNamespace(model="opus", allowed_tools="", session_mode="resume"), None)
    assert "--resume" not in nosid


def test_claude_cmd_force_resume_overrides_fresh():
    # a transient RETRY resumes the captured session even in fresh mode (continue, don't re-run)
    args = SimpleNamespace(model="opus", allowed_tools="", session_mode="fresh")
    cmd = poller.build_claude_cmd(args, "sess-9", force_resume=True)
    assert "--resume" in cmd and "sess-9" in cmd
    # but force_resume with no session id still can't resume
    assert "--resume" not in poller.build_claude_cmd(args, None, force_resume=True)


def test_claude_cmd_passes_through_extra_flags():
    args = SimpleNamespace(model="opus", allowed_tools="mcp__atmcp", session_mode="resume",
                           claude_args="--add-dir /repo --add-dir /shared --permission-mode acceptEdits")
    cmd = poller.build_claude_cmd(args, "sess-1")
    assert cmd.count("--add-dir") == 2 and "/repo" in cmd and "/shared" in cmd
    assert "--permission-mode" in cmd and "acceptEdits" in cmd


def test_claude_cmd_without_extra_flags_still_works():
    cmd = poller.build_claude_cmd(SimpleNamespace(model="opus", allowed_tools="", session_mode="resume", claude_args=""), None)
    assert cmd[0] == "claude" and "-p" in cmd


def test_custom_cmd_is_argv_safe_against_injection():
    evil = "x; rm -rf / #"
    cmd = poller.build_custom_cmd("codex exec --last {prompt}", evil)
    assert cmd[:3] == ["codex", "exec", "--last"]
    # the malicious text stays inside exactly ONE argv element (no shell parsing)
    assert sum(1 for p in cmd if evil in p) == 1
    assert evil in cmd[-1]


def test_custom_cmd_appends_prompt_without_token():
    cmd = poller.build_custom_cmd("mytool run", "hello")
    assert cmd[:2] == ["mytool", "run"]
    assert "hello" in cmd[-1]


# ── Windows .cmd shim resolution ─────────────────────────────────────────────
def test_win_wrap_cmd_shim_runs_via_cmd(monkeypatch):
    monkeypatch.setattr(poller.os, "name", "nt")
    monkeypatch.setattr(poller.shutil, "which",
                        lambda n: r"C:\Users\me\AppData\Roaming\npm\claude.cmd")
    out = poller._win_wrap(["claude", "-p", "--model", "opus"])
    assert out[:2] == ["cmd", "/c"]
    assert out[2].lower().endswith("claude.cmd")
    assert out[3:] == ["-p", "--model", "opus"]  # flags preserved, in order


def test_win_wrap_exe_used_by_full_path_no_shell(monkeypatch):
    monkeypatch.setattr(poller.os, "name", "nt")
    monkeypatch.setattr(poller.shutil, "which", lambda n: r"C:\Program Files\Claude\claude.exe")
    out = poller._win_wrap(["claude", "-p"])
    assert out[0].lower().endswith("claude.exe") and out[0] != "cmd"
    assert out[1:] == ["-p"]


def test_win_wrap_noop_on_posix(monkeypatch):
    monkeypatch.setattr(poller.os, "name", "posix")
    assert poller._win_wrap(["claude", "-p", "--model", "opus"]) == ["claude", "-p", "--model", "opus"]


def test_win_wrap_missing_binary_left_for_clear_error(monkeypatch):
    monkeypatch.setattr(poller.os, "name", "nt")
    monkeypatch.setattr(poller.shutil, "which", lambda n: None)
    assert poller._win_wrap(["claude", "-p"]) == ["claude", "-p"]  # subprocess raises "not found"


# ── usage accounting + budget brake ──────────────────────────────────────────
def test_extract_usage_pulls_tokens_and_cost():
    j = {
        "result": "done", "session_id": "s1", "num_turns": 4, "duration_ms": 8000,
        "total_cost_usd": 0.37,
        "usage": {"input_tokens": 1200, "output_tokens": 450,
                  "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 300},
        "model": "claude-opus-4-8",
    }
    u = poller._extract_usage(j, "opus")
    assert u["input_tokens"] == 1200 and u["output_tokens"] == 450
    assert u["cache_read"] == 9000 and u["cache_creation"] == 300
    assert u["cost_usd"] == 0.37 and u["num_turns"] == 4
    assert u["model"] == "claude-opus-4-8"  # prefers the model the CLI reports


def test_extract_usage_none_when_empty():
    assert poller._extract_usage({"result": "x", "session_id": "s"}, "opus") is None


def test_over_budget_cost_and_tokens():
    cost_only = SimpleNamespace(cost_budget=1.0, token_budget=0)
    assert poller.over_budget(cost_only, 0.5, 10_000) is None
    assert poller.over_budget(cost_only, 1.0, 0) is not None      # at the cap → paused
    tok_only = SimpleNamespace(cost_budget=0, token_budget=5000)
    assert poller.over_budget(tok_only, 99.0, 4999) is None
    assert poller.over_budget(tok_only, 0, 5000) is not None
    unlimited = SimpleNamespace(cost_budget=0, token_budget=0)
    assert poller.over_budget(unlimited, 1e9, 1_000_000_000) is None


def test_usage_totals_persist_and_merge_with_session(tmp_path):
    args = SimpleNamespace(team="t", name="bob", state_dir=str(tmp_path), session_mode="resume")
    # session id and usage totals share one file without clobbering each other
    poller.save_session_id(args, "sess-xyz")
    poller.save_usage_totals(args, 1.23, 4567)
    poller.save_session_id(args, "sess-xyz")  # re-save session must not wipe usage
    assert poller.load_session_id(args) == "sess-xyz"
    assert poller.load_usage_totals(args) == (1.23, 4567)


def test_reset_usage_zeroes_totals(tmp_path):
    args = SimpleNamespace(team="t", name="bob", state_dir=str(tmp_path), session_mode="resume")
    poller.save_usage_totals(args, 9.99, 1000)
    poller.save_usage_totals(args, 0.0, 0)
    assert poller.load_usage_totals(args) == (0.0, 0)


# ── transient-failure retry classification + backoff ─────────────────────────
def test_is_retriable_transient_failures():
    for t in [
        "API Error: 529 overloaded_error",
        "Error 503 Service Unavailable",
        "rate_limit_error: too many requests",
        "Connection reset by peer",
        "fetch failed",
        "executor timed out",
        "API Error: 502 Bad Gateway",
        "Overloaded, please retry your request",
    ]:
        assert poller.is_retriable(t), t


def test_is_retriable_permanent_failures():
    for t in [
        "API Error: 401 authentication_error",   # auth → never retry
        "API Error: 403 permission denied",
        "API Error: 400 invalid_request_error",
        "command blocked by guard",
        "executor not found: claude",
        "the test suite reports 3 failing assertions",  # a real, correct 'failed' result
        "",
    ]:
        assert not poller.is_retriable(t), t


def test_is_retriable_does_not_match_unrelated_digits():
    # a benign result that happens to contain a 5xx-looking number must not trigger retry
    assert not poller.is_retriable("processed 5123 records successfully")  # 5123 not a bare 5xx token
    assert poller.is_retriable("got HTTP 500 from the model API")          # bare 500 does


def test_retry_delay_grows_and_caps():
    base = 2.0
    d1 = poller.retry_delay(1, base)
    d3 = poller.retry_delay(3, base)
    assert 1.0 <= d1 <= 3.0          # 2 × [0.5,1.5]
    assert d3 <= 60.0 * 1.5          # capped at 60 before jitter
    # a late attempt is bounded by the cap, never unbounded
    assert poller.retry_delay(20, base) <= 60.0 * 1.5
