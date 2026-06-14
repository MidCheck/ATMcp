"""Poller command builders: resume wiring + argv-safety (no shell injection)."""

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
    cmd = poller.build_claude_cmd(args, "do x", "sess-123")
    assert "--resume" in cmd and "sess-123" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "do x" in cmd[-1]


def test_claude_cmd_no_resume_when_fresh_or_no_sid():
    fresh = poller.build_claude_cmd(SimpleNamespace(model="opus", allowed_tools="", session_mode="fresh"), "x", "s1")
    assert "--resume" not in fresh
    nosid = poller.build_claude_cmd(SimpleNamespace(model="opus", allowed_tools="", session_mode="resume"), "x", None)
    assert "--resume" not in nosid


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
