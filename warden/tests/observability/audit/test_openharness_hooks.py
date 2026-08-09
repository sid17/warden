"""Unit tests for OpenHarness audit hook handler and registry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


from warden.observability.audit.openharness_hook_handler import AuditLogWriter, handle_payload, main
from warden.observability.audit.openharness_hooks import build_openharness_audit_hooks


# ---------------------------------------------------------------------------
# handle_payload — event type mapping
# ---------------------------------------------------------------------------

class TestHandlePayload:
    def test_pre_tool_use(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "pre_tool_use",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "echo hello"},
        }
        event = handle_payload(payload)
        assert event.event_type == "PreToolUse"
        assert event.tool_name == "run_shell_command"
        assert event.tool_input_summary == {"command": "echo hello"}
        assert event.run_id == "test-run"

    def test_post_tool_use_success(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "post_tool_use",
            "tool_name": "read_file",
            "tool_input": {"path": "/tmp/foo.py"},
            "tool_output": "file contents here",
            "tool_is_error": False,
        }
        event = handle_payload(payload)
        assert event.event_type == "PostToolUse"
        assert event.tool_name == "read_file"
        assert event.tool_output_summary == "file contents here"

    def test_post_tool_use_failure(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "post_tool_use",
            "tool_name": "read_file",
            "tool_input": {"path": "/nonexistent"},
            "tool_output": "File not found",
            "tool_is_error": True,
        }
        event = handle_payload(payload)
        assert event.event_type == "PostToolUseFailure"
        assert event.error == "File not found"

    def test_subagent_stop(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "subagent_stop",
            "agent_id": "research-agent",
            "task_id": "task-1",
            "status": "completed",
            "return_code": 0,
        }
        event = handle_payload(payload)
        assert event.event_type == "SubagentStop"
        assert event.agent_id == "research-agent"

    def test_stop(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "stop",
            "stop_reason": "end_turn",
        }
        event = handle_payload(payload)
        assert event.event_type == "Stop"
        assert event.stop_reason == "end_turn"

    def test_notification(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "notification",
            "notification_type": "permission",
            "tool_name": "run_shell_command",
            "reason": "requires approval",
        }
        event = handle_payload(payload)
        assert event.event_type == "Notification"
        assert event.notification_type == "permission"
        assert event.tool_name == "run_shell_command"

    def test_unknown_event(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {"event": "some_new_event"}
        event = handle_payload(payload)
        assert event.event_type == "some_new_event"

    def test_default_run_id(self, monkeypatch):
        monkeypatch.delenv("AUDIT_RUN_ID", raising=False)
        payload = {"event": "stop", "stop_reason": "end_turn"}
        event = handle_payload(payload)
        assert event.run_id == "run-default"


# ---------------------------------------------------------------------------
# JSONL output format — OTel dot-notation keys match Claude SDK
# ---------------------------------------------------------------------------

class TestJsonlFormat:
    def test_otel_dot_notation_keys(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "pre_tool_use",
            "tool_name": "read_file",
            "tool_input": {"path": "/tmp/x.py"},
        }
        event = handle_payload(payload)
        d = event.to_jsonl_dict()
        assert "gen_ai.operation.name" in d
        assert d["gen_ai.operation.name"] == "execute_tool"
        assert "gen_ai.tool.name" in d
        assert d["gen_ai.tool.name"] == "read_file"
        # Underscored keys must not be present
        assert "gen_ai_operation_name" not in d
        assert "gen_ai_tool_name" not in d

    def test_valid_json_line(self, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "test-run")
        payload = {
            "event": "post_tool_use",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "ls"},
            "tool_output": "file1\nfile2",
            "tool_is_error": False,
        }
        event = handle_payload(payload)
        line = event.to_jsonl_line()
        parsed = json.loads(line)
        assert parsed["event_type"] == "PostToolUse"
        assert parsed["tool_name"] == "run_shell_command"


# ---------------------------------------------------------------------------
# main() — end-to-end via env var
# ---------------------------------------------------------------------------

class TestMain:
    def test_writes_jsonl_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "oh-test")
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        monkeypatch.setenv(
            "OPENHARNESS_HOOK_PAYLOAD",
            json.dumps({
                "event": "pre_tool_use",
                "tool_name": "bash",
                "tool_input": {"command": "echo hi"},
            }),
        )
        main()
        log_file = tmp_path / "oh-test.jsonl"
        assert log_file.exists()
        line = json.loads(log_file.read_text().strip())
        assert line["event_type"] == "PreToolUse"
        assert line["tool_name"] == "bash"

    def test_no_payload_does_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
        monkeypatch.delenv("OPENHARNESS_HOOK_PAYLOAD", raising=False)
        main()
        assert list(tmp_path.iterdir()) == []

    def test_invalid_json_does_not_crash(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_HOOK_PAYLOAD", "not-json{{{")
        # Should not raise — audit must never block the pipeline
        main()


# ---------------------------------------------------------------------------
# AuditLogWriter
# ---------------------------------------------------------------------------

class TestAuditLogWriter:
    def test_appends_to_correct_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_RUN_ID", "run-42")
        writer = AuditLogWriter(tmp_path)
        payload = {
            "event": "pre_tool_use",
            "tool_name": "bash",
            "tool_input": {},
        }
        event = handle_payload(payload)
        writer.append(event)
        writer.append(event)
        log_file = tmp_path / "run-42.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# build_openharness_audit_hooks — registry validation
# ---------------------------------------------------------------------------

class TestBuildAuditHooks:
    def test_returns_hook_executor(self):
        from openharness.hooks.executor import HookExecutor

        mock_client = MagicMock()
        executor = build_openharness_audit_hooks(
            cwd=Path("."),
            api_client=mock_client,
            model="qwen3:1.7b",
        )
        assert isinstance(executor, HookExecutor)

    def test_registers_five_events(self):
        from openharness.hooks.events import HookEvent

        mock_client = MagicMock()
        executor = build_openharness_audit_hooks(
            cwd=Path("."),
            api_client=mock_client,
            model="qwen3:1.7b",
        )
        # Access the internal registry to verify
        registry = executor._registry
        expected_events = [
            HookEvent.PRE_TOOL_USE,
            HookEvent.POST_TOOL_USE,
            HookEvent.SUBAGENT_STOP,
            HookEvent.STOP,
            HookEvent.NOTIFICATION,
        ]
        for event in expected_events:
            hooks = registry.get(event)
            assert len(hooks) == 1, f"Expected 1 hook for {event}, got {len(hooks)}"
            assert hooks[0].block_on_failure is False
            assert hooks[0].matcher is None

    def test_no_hooks_for_other_events(self):
        from openharness.hooks.events import HookEvent

        mock_client = MagicMock()
        executor = build_openharness_audit_hooks(
            cwd=Path("."),
            api_client=mock_client,
            model="qwen3:1.7b",
        )
        registry = executor._registry
        # These events should NOT have hooks
        for event in [HookEvent.SESSION_START, HookEvent.SESSION_END,
                      HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT,
                      HookEvent.USER_PROMPT_SUBMIT]:
            assert registry.get(event) == []
