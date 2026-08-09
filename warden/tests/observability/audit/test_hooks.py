"""Unit tests for audit hook callbacks and JSONL output."""

from __future__ import annotations

import asyncio
import json


from warden.observability.audit.claude_sdk_hooks import AuditLogWriter, build_audit_hooks
from warden.schemas.audit import AuditEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context():
    """Minimal HookContext-like dict."""
    return {"signal": None}


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _closured_hook(run_id: str, log_dir):
    """Obtain the closurized audit callback (run_id + log_dir baked in)."""
    return build_audit_hooks(run_id=run_id, log_dir=log_dir)["PreToolUse"][0].hooks[0]


# ---------------------------------------------------------------------------
# AuditEvent.summarize_tool_input
# ---------------------------------------------------------------------------

class TestSummarizeToolInput:
    def test_write_drops_content(self):
        result = AuditEvent.summarize_tool_input(
            {"file_path": "/tmp/foo.py", "content": "x" * 5000}, "Write"
        )
        assert "file_path" in result
        assert "content" not in result

    def test_edit_drops_old_new_string(self):
        result = AuditEvent.summarize_tool_input(
            {"file_path": "/tmp/foo.py", "old_string": "a", "new_string": "b"}, "Edit"
        )
        assert "file_path" in result
        assert "old_string" not in result
        assert "new_string" not in result

    def test_bash_truncates_command(self):
        long_cmd = "echo " + "x" * 300
        result = AuditEvent.summarize_tool_input({"command": long_cmd}, "Bash")
        assert len(result["command"]) <= 204  # 200 + "..."

    def test_read_keeps_file_path(self):
        result = AuditEvent.summarize_tool_input(
            {"file_path": "/tmp/foo.py"}, "Read"
        )
        assert result["file_path"] == "/tmp/foo.py"

    def test_default_truncates_long_values(self):
        result = AuditEvent.summarize_tool_input(
            {"data": "y" * 200}, "SomeTool"
        )
        assert result["data"].endswith("...")
        assert len(result["data"]) <= 104  # 100 + "..."


# ---------------------------------------------------------------------------
# AuditEvent.summarize_tool_output
# ---------------------------------------------------------------------------

class TestSummarizeToolOutput:
    def test_short_output_unchanged(self):
        assert AuditEvent.summarize_tool_output("hello") == "hello"

    def test_long_output_truncated(self):
        long_str = "z" * 300
        result = AuditEvent.summarize_tool_output(long_str)
        assert result.startswith("z" * 100)
        assert "300 bytes" in result


# ---------------------------------------------------------------------------
# AuditEvent.to_jsonl_dict — OTel dot-notation keys
# ---------------------------------------------------------------------------

class TestToJsonlDict:
    def test_otel_dot_keys(self):
        event = AuditEvent(
            event_type="PreToolUse",
            timestamp="2026-06-17T10:00:00Z",
            run_id="run-1",
            session_id="sess-1",
            gen_ai_operation_name="execute_tool",
            gen_ai_agent_name="builder",
            gen_ai_tool_name="Write",
        )
        d = event.to_jsonl_dict()
        assert "gen_ai.operation.name" in d
        assert d["gen_ai.operation.name"] == "execute_tool"
        assert "gen_ai.agent.name" in d
        assert d["gen_ai.agent.name"] == "builder"
        assert "gen_ai.tool.name" in d
        assert d["gen_ai.tool.name"] == "Write"
        # Original underscored keys should NOT be present
        assert "gen_ai_operation_name" not in d

    def test_none_fields_excluded(self):
        event = AuditEvent(
            event_type="Stop",
            timestamp="2026-06-17T10:00:00Z",
            run_id="run-1",
            session_id="sess-1",
        )
        d = event.to_jsonl_dict()
        assert "tool_name" not in d
        assert "error" not in d

    def test_valid_json(self):
        event = AuditEvent(
            event_type="PreToolUse",
            timestamp="2026-06-17T10:00:00Z",
            run_id="run-1",
            session_id="sess-1",
            tool_name="Bash",
            gen_ai_tool_name="Bash",
        )
        line = event.to_jsonl_line()
        parsed = json.loads(line)
        assert parsed["event_type"] == "PreToolUse"


# ---------------------------------------------------------------------------
# audit_hook callback
# ---------------------------------------------------------------------------

class TestAuditHook:
    def test_pre_tool_use(self, tmp_path):
        hook = _closured_hook("test-run", tmp_path)
        hook_input = {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "agent_id": "builder",
            "agent_type": "main",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.py", "content": "big content"},
            "tool_use_id": "tu-1",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/workspace",
        }
        result = _run(hook(hook_input, "tu-1", _make_context()))
        assert result == {}
        # Verify JSONL was written
        log_file = tmp_path / "test-run.jsonl"
        assert log_file.exists()
        line = json.loads(log_file.read_text().strip())
        assert line["event_type"] == "PreToolUse"
        assert line["tool_name"] == "Write"
        assert "content" not in line["tool_input_summary"]
        assert line["tool_input_summary"]["file_path"] == "/tmp/x.py"

    def test_post_tool_use(self, tmp_path):
        hook = _closured_hook("test-run", tmp_path)
        hook_input = {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-1",
            "agent_id": "builder",
            "agent_type": "main",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "tool_response": "hello\n",
            "tool_use_id": "tu-2",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/workspace",
        }
        result = _run(hook(hook_input, "tu-2", _make_context()))
        assert result == {}
        log_file = tmp_path / "test-run.jsonl"
        line = json.loads(log_file.read_text().strip())
        assert line["tool_output_summary"] == "hello\n"

    def test_subagent_start(self, tmp_path):
        hook = _closured_hook("test-run", tmp_path)
        hook_input = {
            "hook_event_name": "SubagentStart",
            "session_id": "sess-1",
            "agent_id": "research-agent",
            "agent_type": "sub-agent",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/workspace",
        }
        result = _run(hook(hook_input, None, _make_context()))
        assert result == {}
        log_file = tmp_path / "test-run.jsonl"
        line = json.loads(log_file.read_text().strip())
        assert line["agent_id"] == "research-agent"
        assert line["agent_type"] == "sub-agent"

    def test_subagent_stop(self, tmp_path):
        hook = _closured_hook("test-run", tmp_path)
        hook_input = {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-1",
            "agent_id": "research-agent",
            "agent_type": "sub-agent",
            "agent_transcript_path": "/tmp/subagent.jsonl",
            "stop_hook_active": False,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/workspace",
        }
        result = _run(hook(hook_input, None, _make_context()))
        assert result == {}
        log_file = tmp_path / "test-run.jsonl"
        line = json.loads(log_file.read_text().strip())
        assert line["agent_id"] == "research-agent"
        assert line["transcript_path"] == "/tmp/subagent.jsonl"

    def test_run_id_default(self, tmp_path):
        # No run_id passed -> signature default "run-default" is closurized.
        hook = build_audit_hooks(log_dir=tmp_path)["PreToolUse"][0].hooks[0]
        hook_input = {
            "hook_event_name": "Stop",
            "session_id": "sess-1",
            "stop_hook_active": False,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/workspace",
        }
        result = _run(hook(hook_input, None, _make_context()))
        assert result == {}
        log_file = tmp_path / "run-default.jsonl"
        assert log_file.exists()


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

class TestAuditLogWriter:
    def test_appends_to_correct_file(self, tmp_path):
        writer = AuditLogWriter(tmp_path)
        event = AuditEvent(
            event_type="PreToolUse",
            timestamp="2026-06-17T10:00:00Z",
            run_id="run-42",
            session_id="sess-1",
            tool_name="Write",
        )
        writer.append(event)
        writer.append(event)
        log_file = tmp_path / "run-42.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert parsed["event_type"] == "PreToolUse"


# ---------------------------------------------------------------------------
# build_audit_hooks
# ---------------------------------------------------------------------------

class TestBuildAuditHooks:
    def test_returns_all_event_types(self):
        hooks = build_audit_hooks()
        expected = [
            "PreToolUse", "PostToolUse", "PostToolUseFailure",
            "SubagentStart", "SubagentStop", "Stop", "Notification",
        ]
        assert sorted(hooks.keys()) == sorted(expected)

    def test_each_has_one_matcher(self):
        hooks = build_audit_hooks()
        for event_type, matchers in hooks.items():
            assert len(matchers) == 1
            assert matchers[0].matcher is None
            assert len(matchers[0].hooks) == 1
            assert matchers[0].timeout == 5.0
