import logging
import time
import uuid
from typing import Any

from claude_agent_sdk import ResultMessage, StreamEvent

try:
    from anthropic.types import (
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
        ToolResultBlock,
    )
    _HAS_ANTHROPIC_TYPES = True
except ImportError:
    _HAS_ANTHROPIC_TYPES = False

logger = logging.getLogger(__name__)


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def _block_kind(block: Any) -> str | None:
    """Determine the kind for a content block using isinstance when possible."""
    if _HAS_ANTHROPIC_TYPES:
        if isinstance(block, TextBlock):
            return "text"
        if isinstance(block, ThinkingBlock):
            return "thinking"
        if isinstance(block, ToolUseBlock):
            return "tool_use"
        if isinstance(block, ToolResultBlock):
            return "tool_result"
    # Fallback: class name check (covers ServerToolUseBlock and missing imports)
    name = type(block).__name__
    return {
        "TextBlock": "text",
        "ThinkingBlock": "thinking",
        "ToolUseBlock": "tool_use",
        "ServerToolUseBlock": "tool_use",
        "ToolResultBlock": "tool_result",
    }.get(name)


def _process_content_blocks(msg: Any, session_id: str) -> list[dict]:
    """Extract ChatMessage dicts from content blocks."""
    messages: list[dict] = []
    content = getattr(msg, "content", None)
    if not content:
        return messages

    for block in content:
        kind = _block_kind(block)
        if kind == "text":
            messages.append({
                "kind": "text",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": block.text,
            })
        elif kind == "thinking":
            messages.append({
                "kind": "thinking",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": block.thinking,
            })
        elif kind == "tool_use":
            messages.append({
                "kind": "tool_use",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "toolName": block.name,
                "toolCallId": block.id,
                "toolInput": block.input,
            })
        elif kind == "tool_result":
            messages.append({
                "kind": "tool_result",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "toolCallId": block.tool_use_id,
                "toolResult": block.content,
                "isError": getattr(block, "is_error", False),
            })
        else:
            logger.info("Skipping unknown content block: %s", type(block).__name__)

    return messages


def transform_sdk_message(msg: Any, session_id: str) -> list[dict]:
    """Convert a single SDK message into a list of ChatMessage dicts."""
    # Stream events (partial messages)
    if isinstance(msg, StreamEvent):
        event = msg.event
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                return [{
                    "kind": "stream_delta",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "text": delta.get("text", ""),
                }]
        elif event_type == "content_block_stop":
            return [{
                "kind": "stream_end",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
            }]
        elif event_type == "message_delta":
            # B20a — the mid-turn cost signal: message_delta frames carry a
            # cumulative ``output_tokens`` as the turn generates. Surface it as a
            # normalized ``usage_delta`` so the orchestrator can run a mid_stream
            # governor.check() (the Claude intra-turn tripwire, 3e). No usage on
            # the frame ⇒ nothing to report.
            usage = event.get("usage")
            if usage:
                return [{
                    "kind": "usage_delta",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "usage": dict(usage),
                }]
        return []

    # Content blocks (AssistantMessage and variants)
    if hasattr(msg, "content") and isinstance(getattr(msg, "content", None), list):
        return _process_content_blocks(msg, session_id)

    if isinstance(msg, ResultMessage) or type(msg).__name__ == "ResultMessage":
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": "result",
            "subtype": "result",
            "durationMs": getattr(msg, "duration_ms", None),
            "isError": getattr(msg, "is_error", False),
            "numTurns": getattr(msg, "num_turns", None),
            "totalCostUsd": getattr(msg, "total_cost_usd", None),
            "usage": getattr(msg, "usage", None),
        }]

    if hasattr(msg, "error") and msg.error:
        return [{
            "kind": "error",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": str(msg.error),
            "isError": True,
        }]

    logger.info("Skipping unhandled SDK message: %s", type(msg).__name__)
    return []
