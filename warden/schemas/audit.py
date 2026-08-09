"""AuditEvent dataclass with OTel-aligned fields and JSONL serialization."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class AuditEvent:
    """A single audit event captured by SDK callback hooks."""

    event_type: str
    timestamp: str  # ISO 8601 UTC
    run_id: str
    session_id: str
    agent_id: str | None = None
    agent_type: str | None = None
    parent_session_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input_summary: dict | None = None
    tool_output_summary: str | None = None
    # OTel GenAI semantic convention fields
    gen_ai_operation_name: str = "execute_tool"
    gen_ai_agent_name: str | None = None
    gen_ai_tool_name: str | None = None
    # PostToolUseFailure fields
    error: str | None = None
    is_interrupt: bool | None = None
    # SubagentStop fields
    transcript_path: str | None = None
    # Stop fields
    stop_reason: str | None = None
    # Notification fields
    notification_message: str | None = None
    notification_type: str | None = None
    notification_title: str | None = None

    def to_jsonl_dict(self) -> dict:
        """Serialize to JSON dict with OTel dot-notation keys."""
        d: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            # Map gen_ai_ prefixed fields to dot-notation
            if k.startswith("gen_ai_"):
                dot_key = k.replace("_", ".", 2)  # gen_ai_operation_name -> gen_ai.operation.name
                # Handle remaining underscores in the suffix
                parts = k.split("_", 2)  # ['gen', 'ai', 'operation_name']
                dot_key = f"gen_ai.{parts[2].replace('_', '.')}"
                d[dot_key] = v
            else:
                d[k] = v
        return d

    def to_jsonl_line(self) -> str:
        """Serialize to a single JSONL line."""
        return json.dumps(self.to_jsonl_dict(), separators=(",", ":"))

    @staticmethod
    def summarize_tool_input(tool_input: dict[str, Any], tool_name: str) -> dict:
        """Summarize tool input, dropping large content fields."""
        if tool_name in ("Write", "Edit", "MultiEdit"):
            return {
                k: v
                for k, v in tool_input.items()
                if k not in ("content", "new_string", "old_string")
            }
        if tool_name == "Bash":
            summary = dict(tool_input)
            if "command" in summary and len(str(summary["command"])) > 200:
                summary["command"] = str(summary["command"])[:200] + "..."
            return summary
        # Default: keep all keys, truncate long values
        summary = {}
        for k, v in tool_input.items():
            s = str(v)
            summary[k] = s[:100] + "..." if len(s) > 100 else v
        return summary

    @staticmethod
    def summarize_tool_output(tool_response: Any) -> str:
        """Summarize tool output — truncate to first 100 chars."""
        s = str(tool_response)
        if len(s) <= 100:
            return s
        return f"{s[:100]}... ({len(s)} bytes)"
