import time
import uuid
from typing import Any

from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


# Track tool_use IDs so we can link ToolExecutionCompleted back to its start.
# Keyed by (session_id, tool_name) so concurrent sessions in the same process
# (the Runner allows N concurrent runs) don't collide and mismatch ids.
# Value: toolCallId we generated. Entries are popped on completion to avoid
# unbounded growth.
_pending_tool_ids: dict[tuple[str, str], str] = {}


def transform_openharness_message(event: Any, session_id: str) -> list[dict]:
    """Convert an OpenHarness StreamEvent into ChatMessage dicts.

    Event types from openharness.engine.stream_events:
      AssistantTextDelta(text)
      AssistantTurnComplete(message, usage)
      ToolExecutionStarted(tool_name, tool_input)
      ToolExecutionCompleted(tool_name, output, is_error)
      ErrorEvent(message, recoverable)
      StatusEvent(message)
      CompactProgressEvent(phase, trigger, message, ...)
    """
    if isinstance(event, AssistantTextDelta):
        return [{
            "kind": "stream_delta",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": event.text,
        }]

    if isinstance(event, ToolExecutionStarted):
        tool_call_id = _make_id()
        _pending_tool_ids[(session_id, event.tool_name)] = tool_call_id
        return [{
            "kind": "tool_use",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "toolName": event.tool_name,
            "toolCallId": tool_call_id,
            "toolInput": event.tool_input,
        }]

    if isinstance(event, ToolExecutionCompleted):
        tool_call_id = _pending_tool_ids.pop((session_id, event.tool_name), "")
        return [{
            "kind": "tool_result",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "toolCallId": tool_call_id,
            "toolResult": event.output,
            "isError": event.is_error,
        }]

    if isinstance(event, AssistantTurnComplete):
        usage = event.usage
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": "result",
            "subtype": "result",
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        }]

    if isinstance(event, ErrorEvent):
        return [{
            "kind": "error",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": event.message,
            "isError": True,
        }]

    if isinstance(event, StatusEvent):
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": event.message,
        }]

    if isinstance(event, CompactProgressEvent):
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": event.message or f"compaction: {event.phase}",
        }]

    # Unknown event type — skip
    return []
