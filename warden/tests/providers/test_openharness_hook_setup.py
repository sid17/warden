"""Tests for build_openharness_hook_executor (M5 3a-2).

Hermetic: the heavy builders (audit command-hooks, permission bridge) are stubbed
via monkeypatch so no OpenHarness engine or subprocess is constructed. These lock
in that the OpenHarness audit path is CONFIG-gated (AuditConfig.enabled), NOT
env-gated (AUDIT_ENABLED), and that AUDIT_RUN_ID / AUDIT_LOG_DIR are DERIVED from
config and injected into the process env at the subprocess boundary.
"""

import os
from pathlib import Path

from warden.config.models import AuditConfig
from warden.providers.openharness.hook_setup import (
    build_openharness_hook_executor,
)


def test_config_gated_not_env_gated(tmp_path, monkeypatch):
    """AUDIT_ENABLED=1 in env must NOT enable auditing; only config.enabled does."""
    monkeypatch.setenv("AUDIT_ENABLED", "1")

    calls = []

    def _fake_audit_builder(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(
        "warden.observability.audit.openharness_hooks.build_openharness_audit_hooks",
        _fake_audit_builder,
    )

    result = build_openharness_hook_executor(
        can_use_tool=None,
        audit=AuditConfig(enabled=False),
        repo_path=tmp_path,
        api_client=object(),
        model="m",
    )

    assert result is None
    assert calls == []  # audit builder never called despite AUDIT_ENABLED=1


def test_enabled_injects_run_id_and_log_dir(tmp_path, monkeypatch):
    """audit.enabled derives AUDIT_RUN_ID / AUDIT_LOG_DIR into the process env."""
    monkeypatch.delenv("AUDIT_ENABLED", raising=False)
    monkeypatch.delenv("AUDIT_RUN_ID", raising=False)
    monkeypatch.delenv("AUDIT_LOG_DIR", raising=False)

    sentinel = object()

    def _fake_audit_builder(**kwargs):
        return sentinel

    monkeypatch.setattr(
        "warden.observability.audit.openharness_hooks.build_openharness_audit_hooks",
        _fake_audit_builder,
    )

    result = build_openharness_hook_executor(
        can_use_tool=None,
        audit=AuditConfig(enabled=True, run_id="run-xyz", log_dir="/tmp/audlogs"),
        repo_path=tmp_path,
        api_client=object(),
        model="m",
    )

    assert os.environ["AUDIT_RUN_ID"] == "run-xyz"
    assert os.environ["AUDIT_LOG_DIR"] == "/tmp/audlogs"
    assert result is sentinel  # can_use_tool=None -> audit executor returned as-is


def test_permission_only_path(tmp_path, monkeypatch):
    """audit=None + a can_use_tool -> PermissionHookExecutor with no audit executor."""
    hook_sentinel = object()
    captured = {}

    def _fake_build_permission_hook(cut):
        captured["can_use_tool"] = cut
        return hook_sentinel

    class _FakeExecutor:
        def __init__(self, hook, audit_executor=None):
            captured["hook"] = hook
            captured["audit_executor"] = audit_executor

    monkeypatch.setattr(
        "warden.providers.openharness.permission_bridge.build_permission_hook",
        _fake_build_permission_hook,
    )
    monkeypatch.setattr(
        "warden.providers.openharness.permission_bridge.PermissionHookExecutor",
        _FakeExecutor,
    )

    def _cut():
        return None

    result = build_openharness_hook_executor(
        can_use_tool=_cut,
        audit=None,
        repo_path=tmp_path,
        api_client=object(),
        model="m",
    )

    assert isinstance(result, _FakeExecutor)
    assert captured["can_use_tool"] is _cut
    assert captured["hook"] is hook_sentinel
    assert captured["audit_executor"] is None


def test_no_audit_enabled_gate_in_session_source():
    """The AUDIT_ENABLED env gate must no longer live in openharness/session.py."""
    session_path = (
        Path(__file__).resolve().parents[2]
        / "providers"
        / "openharness"
        / "session.py"
    )
    assert "AUDIT_ENABLED" not in session_path.read_text()
