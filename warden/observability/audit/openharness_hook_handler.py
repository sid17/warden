"""OpenHarness command hook handler — receives payload via env var, writes JSONL.

Invoked as: python -m warden.observability.audit.openharness_hook_handler
The HookExecutor sets $OPENHARNESS_HOOK_PAYLOAD (JSON string) and
$OPENHARNESS_HOOK_EVENT automatically.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from warden.schemas.audit import AuditEvent

logger = logging.getLogger(__name__)


# Reuse AuditLogWriter from claude_sdk_hooks (same append logic)
class AuditLogWriter:
    """Appends AuditEvent lines to per-run JSONL files."""

    def __init__(self, log_dir: Path | None = None):
        self._log_dir = log_dir or Path(__file__).parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def append(self, event: AuditEvent) -> None:
        path = self._log_dir / f"{event.run_id}.jsonl"
        with open(path, "a") as f:
            f.write(event.to_jsonl_line() + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_run_id() -> str:
    return os.environ.get("AUDIT_RUN_ID", "run-default")


def _get_log_dir() -> Path | None:
    d = os.environ.get("AUDIT_LOG_DIR")
    return Path(d) if d else None


# OpenHarness tool names → Claude SDK equivalents for input summarization.
# The summarizer drops content fields for write/edit tools to keep audit logs small.
_TOOL_NAME_MAP = {
    "write_file": "Write",
    "edit_file": "Edit",
    "run_shell_command": "Bash",
}


def _summarizer_tool_name(tool_name: str) -> str:
    """Map OH tool name to the Claude SDK name used by summarize_tool_input."""
    return _TOOL_NAME_MAP.get(tool_name, tool_name)


def handle_payload(payload: dict) -> AuditEvent:
    """Map an OpenHarness hook payload to an AuditEvent."""
    event_name = payload.get("event", "")
    run_id = _get_run_id()
    session_id = payload.get("session_id", "")

    base: dict = dict(
        timestamp=_now_iso(),
        run_id=run_id,
        session_id=session_id,
    )

    if event_name == "pre_tool_use":
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        base.update(
            event_type="PreToolUse",
            tool_name=tool_name,
            tool_input_summary=AuditEvent.summarize_tool_input(tool_input, _summarizer_tool_name(tool_name)),
            gen_ai_operation_name="execute_tool",
            gen_ai_tool_name=tool_name,
        )

    elif event_name == "post_tool_use":
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})
        tool_is_error = payload.get("tool_is_error", False)

        if tool_is_error:
            base.update(
                event_type="PostToolUseFailure",
                tool_name=tool_name,
                tool_input_summary=AuditEvent.summarize_tool_input(tool_input, _summarizer_tool_name(tool_name)),
                error=AuditEvent.summarize_tool_output(payload.get("tool_output", "")),
                gen_ai_operation_name="execute_tool",
                gen_ai_tool_name=tool_name,
            )
        else:
            base.update(
                event_type="PostToolUse",
                tool_name=tool_name,
                tool_input_summary=AuditEvent.summarize_tool_input(tool_input, _summarizer_tool_name(tool_name)),
                tool_output_summary=AuditEvent.summarize_tool_output(
                    payload.get("tool_output", "")
                ),
                gen_ai_operation_name="execute_tool",
                gen_ai_tool_name=tool_name,
            )

    elif event_name == "subagent_stop":
        base.update(
            event_type="SubagentStop",
            agent_id=payload.get("agent_id"),
            gen_ai_operation_name="subagent_stop",
            gen_ai_agent_name=payload.get("agent_id"),
        )

    elif event_name == "stop":
        base.update(
            event_type="Stop",
            gen_ai_operation_name="stop",
            stop_reason=payload.get("stop_reason"),
        )

    elif event_name == "notification":
        base.update(
            event_type="Notification",
            gen_ai_operation_name="notification",
            notification_type=payload.get("notification_type"),
            tool_name=payload.get("tool_name"),
        )

    else:
        # Unknown event — still log it, don't crash
        base.update(event_type=event_name or "Unknown")

    return AuditEvent(**base)


def main() -> None:
    """Entry point when run as `python -m warden.observability.audit.openharness_hook_handler`."""
    try:
        raw = os.environ.get("OPENHARNESS_HOOK_PAYLOAD", "")
        if not raw:
            return

        payload = json.loads(raw)
        event = handle_payload(payload)
        writer = AuditLogWriter(_get_log_dir())
        writer.append(event)

    except Exception:
        # Audit must never block the pipeline — swallow and log
        logger.exception("OpenHarness audit hook handler error")


if __name__ == "__main__":
    main()
