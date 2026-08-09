"""M5 3a-1 — audit is config-first, not env-gated.

N12: build_audit_hooks closurizes run_id + log_dir at build time.
C6a: ClaudeSession.install_hooks gates on config.observability.audit, not env.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


from warden.config.models import AuditConfig
from warden.observability.audit.claude_sdk_hooks import build_audit_hooks
from warden.providers.claude.session import ClaudeSession


def _make_context():
    return {"signal": None}


def _run(coro):
    return asyncio.run(coro)


class TestRunIdClosurized:
    def test_build_time_run_id_wins_over_env(self, tmp_path, monkeypatch):
        # Build with a captured run_id + log_dir; the env should NOT override it.
        hooks = build_audit_hooks(run_id="run-abc", log_dir=tmp_path)
        monkeypatch.setenv("AUDIT_RUN_ID", "DIFFERENT")

        matcher = hooks["PreToolUse"][0]
        callback = matcher.hooks[0]
        hook_input = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "agent_id": "builder",
            "agent_type": "main",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.py", "content": "big content"},
            "tool_use_id": "tu-1",
        }
        result = _run(callback(hook_input, None, _make_context()))
        assert result == {}
        # Captured run_id won — file named run-abc.jsonl in the captured log_dir.
        log_file = tmp_path / "run-abc.jsonl"
        assert log_file.exists()
        assert not (tmp_path / "DIFFERENT.jsonl").exists()
        line = json.loads(log_file.read_text().strip())
        assert line["run_id"] == "run-abc"


class _FakeOptions:
    def __init__(self):
        self.hooks = None


class TestInstallHooksGatedOnConfig:
    def test_disabled_config_installs_nothing_even_with_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_ENABLED", "1")
        session = ClaudeSession(repo_path=tmp_path, audit=AuditConfig(enabled=False))
        opts = _FakeOptions()
        session.install_hooks(opts)
        assert not opts.hooks

    def test_enabled_config_installs_hooks_without_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUDIT_ENABLED", raising=False)
        session = ClaudeSession(repo_path=tmp_path, audit=AuditConfig(enabled=True))
        opts = _FakeOptions()
        session.install_hooks(opts)
        assert opts.hooks
        assert "PreToolUse" in opts.hooks


class TestNoEnvGateInClaudeSource:
    def test_audit_enabled_not_in_claude_session_source(self):
        src = Path(
            "warden/providers/claude/session.py"
        ).read_text()
        assert "AUDIT_ENABLED" not in src
