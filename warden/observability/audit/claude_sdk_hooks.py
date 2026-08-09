"""Claude SDK async callback hooks for all audit event types, JSONL writer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    HookContext,
    HookMatcher,
)

from warden.schemas.audit import AuditEvent

logger = logging.getLogger(__name__)


class AuditLogWriter:
    """Appends AuditEvent lines to per-run JSONL files."""

    def __init__(self, log_dir: Path | None = None):
        self._log_dir = log_dir or Path(__file__).parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def append(self, event: AuditEvent) -> None:
        """Append a single event as a JSONL line to {run_id}.jsonl."""
        path = self._log_dir / f"{event.run_id}.jsonl"
        with open(path, "a") as f:
            f.write(event.to_jsonl_line() + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _record_event(hook_input: Any, run_id: str, writer: AuditLogWriter) -> dict:
    """Build + append one audit event from ``hook_input`` using the PASSED
    ``run_id`` + ``writer`` (M5 3a-1 — closurized at build time, no env at fire).

    Always returns {} — audit hooks must never block the pipeline.
    """
    try:
        event_name: str = hook_input["hook_event_name"]
        session_id = hook_input.get("session_id", "")
        agent_id = hook_input.get("agent_id")
        agent_type = hook_input.get("agent_type")
        base = dict(
            event_type=event_name,
            timestamp=_now_iso(),
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_type=agent_type,
        )

        if event_name == "PreToolUse":
            tool_name = hook_input["tool_name"]
            base.update(
                tool_name=tool_name,
                tool_use_id=hook_input.get("tool_use_id"),
                tool_input_summary=AuditEvent.summarize_tool_input(
                    hook_input.get("tool_input", {}), tool_name
                ),
                gen_ai_operation_name="execute_tool",
                gen_ai_agent_name=agent_id,
                gen_ai_tool_name=tool_name,
            )

        elif event_name == "PostToolUse":
            tool_name = hook_input["tool_name"]
            base.update(
                tool_name=tool_name,
                tool_use_id=hook_input.get("tool_use_id"),
                tool_input_summary=AuditEvent.summarize_tool_input(
                    hook_input.get("tool_input", {}), tool_name
                ),
                tool_output_summary=AuditEvent.summarize_tool_output(
                    hook_input.get("tool_response", "")
                ),
                gen_ai_operation_name="execute_tool",
                gen_ai_agent_name=agent_id,
                gen_ai_tool_name=tool_name,
            )

        elif event_name == "PostToolUseFailure":
            tool_name = hook_input["tool_name"]
            base.update(
                tool_name=tool_name,
                tool_use_id=hook_input.get("tool_use_id"),
                tool_input_summary=AuditEvent.summarize_tool_input(
                    hook_input.get("tool_input", {}), tool_name
                ),
                error=hook_input.get("error"),
                is_interrupt=hook_input.get("is_interrupt"),
                gen_ai_operation_name="execute_tool",
                gen_ai_agent_name=agent_id,
                gen_ai_tool_name=tool_name,
            )

        elif event_name == "SubagentStart":
            base.update(
                gen_ai_operation_name="subagent_start",
                gen_ai_agent_name=agent_id,
            )

        elif event_name == "SubagentStop":
            base.update(
                transcript_path=hook_input.get("agent_transcript_path"),
                gen_ai_operation_name="subagent_stop",
                gen_ai_agent_name=agent_id,
            )

        elif event_name == "Stop":
            base.update(
                gen_ai_operation_name="stop",
                stop_reason=hook_input.get("stop_reason"),
            )

        elif event_name == "Notification":
            base.update(
                gen_ai_operation_name="notification",
                notification_message=hook_input.get("message"),
                notification_type=hook_input.get("notification_type"),
                notification_title=hook_input.get("title"),
            )

        event = AuditEvent(**base)
        writer.append(event)

    except Exception:
        logger.exception("Audit hook error for %s", hook_input.get("hook_event_name", "unknown"))

    return {}


def build_audit_hooks(
    run_id: str = "run-default",
    log_dir: "Path | None" = None,
) -> dict[str, list[HookMatcher]]:
    """Build the hooks dict ready for ClaudeAgentOptions.hooks.

    M5 3a-1: ``run_id`` + ``log_dir`` are CLOSURIZED at build time so the
    callback never reads env at fire time. ``log_dir=None`` uses the default
    ``logs/`` dir.
    """
    event_types = [
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "Notification",
    ]
    writer = AuditLogWriter(log_dir)

    async def _closured_hook(
        hook_input: Any,
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict:
        return await _record_event(hook_input, run_id, writer)

    return {
        et: [HookMatcher(matcher=None, hooks=[_closured_hook], timeout=5.0)]
        for et in event_types
    }
