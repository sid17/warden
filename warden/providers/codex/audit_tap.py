"""Codex audit tap — event-stream-derived audit trail (M5 3b).

CodexSdkSession has no native PreToolUse/PostToolUse hook system (only the
exec/patch approval callback + the notification event stream). So we DERIVE the
audit trail from the normalized event stream `send()` already produces: each
command-execution tool_use/tool_result event is written as the SAME AuditEvent
JSONL the Claude/OpenHarness hook trails emit. Config-gated on AuditConfig; the
run_id + writer are captured at build time (no env read).

Coverage note: this captures Codex's event-stream tool calls (command
executions). MCP custom tools ride the elicitation/approval path and are not
surfaced as tool_use events, so they are not in this trail (documented gap).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from warden.observability.audit.claude_sdk_hooks import AuditLogWriter
from warden.schemas.audit import AuditEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CodexAuditTap:
    """Writes AuditEvents for Codex tool_use/tool_result stream events."""

    def __init__(self, run_id: str, writer: AuditLogWriter):
        self._run_id = run_id
        self._writer = writer
        # tool_result events carry no toolName — remember it from the tool_use by
        # toolCallId so PostToolUse lines carry tool_name like the other providers.
        self._tool_names: dict[str, str] = {}

    @classmethod
    def create(cls, audit: Any) -> "CodexAuditTap | None":
        """Build from an AuditConfig (or None). Returns None when audit is off."""
        if audit is None or not audit.enabled:
            return None
        log_dir = Path(audit.log_dir) if audit.log_dir else None
        return cls(audit.run_id, AuditLogWriter(log_dir))

    def record(self, event: dict) -> None:
        """Given one normalized Codex event dict, append an AuditEvent if it is a
        tool_use / tool_result. Never raises — audit must not break the stream."""
        try:
            kind = event.get("kind")
            if kind == "tool_use":
                tool_name = event.get("toolName", "")
                call_id = event.get("toolCallId")
                if call_id:
                    self._tool_names[call_id] = tool_name
                self._writer.append(AuditEvent(
                    event_type="PreToolUse",
                    timestamp=_now_iso(),
                    run_id=self._run_id,
                    session_id=event.get("sessionId", ""),
                    tool_name=tool_name,
                    tool_use_id=call_id,
                    tool_input_summary=AuditEvent.summarize_tool_input(
                        event.get("toolInput", {}) or {}, tool_name,
                    ),
                    gen_ai_operation_name="execute_tool",
                    gen_ai_tool_name=tool_name,
                ))
            elif kind == "tool_result":
                call_id = event.get("toolCallId")
                tool_name = self._tool_names.get(call_id, "Bash")
                self._writer.append(AuditEvent(
                    event_type="PostToolUse",
                    timestamp=_now_iso(),
                    run_id=self._run_id,
                    session_id=event.get("sessionId", ""),
                    tool_name=tool_name,
                    tool_use_id=call_id,
                    tool_output_summary=AuditEvent.summarize_tool_output(
                        event.get("toolResult", ""),
                    ),
                    error=event.get("toolResult", "") if event.get("isError") else None,
                    gen_ai_operation_name="execute_tool",
                    gen_ai_tool_name=tool_name,
                ))
        except Exception:  # never break the stream on an audit write
            import logging
            logging.getLogger(__name__).exception("Codex audit tap error")
