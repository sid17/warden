"""Codex audit tap — event-stream-derived audit trail (M5 3b).

CodexSdkSession has no native PreToolUse/PostToolUse hooks; its per-tool-call
surface is the normalized event stream. These tests verify the tap writes the
SAME AuditEvent JSONL the other providers emit, config-gated on AuditConfig.
"""

from __future__ import annotations

import json
from pathlib import Path

from warden.config.models import AuditConfig
from warden.providers.codex.audit_tap import CodexAuditTap
from warden.providers.codex.sdk_session import CodexSdkSession


# --- create() gating ------------------------------------------------------


def test_create_returns_none_when_disabled():
    assert CodexAuditTap.create(AuditConfig(enabled=False)) is None


def test_create_returns_none_when_config_none():
    assert CodexAuditTap.create(None) is None


def test_create_returns_tap_when_enabled(tmp_path):
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    assert isinstance(tap, CodexAuditTap)


# --- record() writes -------------------------------------------------------


def test_record_tool_use_writes_pretooluse(tmp_path):
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    tap.record({
        "kind": "tool_use",
        "sessionId": "s",
        "timestamp": "t",
        "toolName": "Bash",
        "toolCallId": "c1",
        "toolInput": {"command": "ls"},
    })
    path = tmp_path / "run-c.jsonl"
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "PreToolUse"
    assert rec["run_id"] == "run-c"
    assert rec["session_id"] == "s"
    assert rec["gen_ai.tool.name"] == "Bash"
    assert rec["gen_ai.operation.name"] == "execute_tool"


def test_record_tool_result_writes_posttooluse(tmp_path):
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    tap.record({
        "kind": "tool_result",
        "sessionId": "s",
        "timestamp": "t",
        "toolCallId": "c1",
        "toolResult": "file.txt",
        "isError": False,
    })
    path = tmp_path / "run-c.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "PostToolUse"
    assert rec["tool_output_summary"] == "file.txt"


def test_record_non_tool_event_writes_nothing(tmp_path):
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    tap.record({"kind": "text", "sessionId": "s", "text": "hi"})
    path = tmp_path / "run-c.jsonl"
    assert not path.exists() or path.read_text() == ""


# --- tool_name correlation across tool_use → tool_result -------------------


def test_tool_result_inherits_tool_name_from_tool_use(tmp_path):
    # tool_result events carry no toolName; the tap must correlate it from the
    # preceding tool_use by toolCallId so PostToolUse lines carry tool_name
    # (validate_jsonl requires it).
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    tap.record({
        "kind": "tool_use",
        "sessionId": "s",
        "timestamp": "t",
        "toolName": "Bash",
        "toolCallId": "c1",
        "toolInput": {"command": "ls"},
    })
    tap.record({
        "kind": "tool_result",
        "sessionId": "s",
        "timestamp": "t",
        "toolCallId": "c1",
        "toolResult": "file.txt",
        "isError": False,
    })
    path = tmp_path / "run-c.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    post = json.loads(lines[1])
    assert post["event_type"] == "PostToolUse"
    assert post["tool_name"] == "Bash"
    assert post["gen_ai.tool.name"] == "Bash"


def test_tool_result_unseen_callid_defaults_to_bash(tmp_path):
    # A tool_result with an unseen toolCallId still carries tool_name == "Bash"
    # (the default), so the PostToolUse line remains valid.
    tap = CodexAuditTap.create(
        AuditConfig(enabled=True, run_id="run-c", log_dir=str(tmp_path))
    )
    tap.record({
        "kind": "tool_result",
        "sessionId": "s",
        "timestamp": "t",
        "toolCallId": "unseen",
        "toolResult": "out",
        "isError": False,
    })
    path = tmp_path / "run-c.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    post = json.loads(lines[0])
    assert post["event_type"] == "PostToolUse"
    assert post["tool_name"] == "Bash"
    assert post["gen_ai.tool.name"] == "Bash"


# --- session ctor wiring ---------------------------------------------------


def test_session_builds_tap_when_audit_enabled(tmp_path):
    s = CodexSdkSession(
        repo_path=Path("."),
        audit=AuditConfig(enabled=True, run_id="r", log_dir=str(tmp_path)),
    )
    assert isinstance(s._audit_tap, CodexAuditTap)


def test_session_no_tap_when_audit_none():
    s = CodexSdkSession(repo_path=Path("."))
    assert s._audit_tap is None


def test_session_no_tap_when_audit_disabled():
    s = CodexSdkSession(repo_path=Path("."), audit=AuditConfig(enabled=False))
    assert s._audit_tap is None
