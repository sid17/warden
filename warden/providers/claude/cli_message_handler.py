"""Transform Claude CLI NDJSON events into ChatMessage dicts.

Real event format from `claude -p --output-format stream-json --verbose`:
  {"type":"system","subtype":"init","session_id":"...","model":"...","tools":[...]}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}],...},"session_id":"..."}
  {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."},...}
  {"type":"content_block_stop",...}
  {"type":"result","subtype":"success","result":"...","session_id":"...","usage":{...},...}
  {"type":"rate_limit_event",...}
"""

import logging
import time
import uuid

logger = logging.getLogger(__name__)


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def transform_cli_message(event: dict, session_id: str) -> list[dict]:
    """Convert a single Claude CLI NDJSON event into ChatMessage dicts."""
    event_type = event.get("type", "")

    # --- system/init: session initialization ---
    if event_type == "system":
        subtype = event.get("subtype", "")
        if subtype == "init":
            return [{
                "kind": "status",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": "init",
                "subtype": "init",
                "model": event.get("model"),
                "tools": event.get("tools", []),
            }]
        return []

    # --- assistant: full message with content blocks ---
    if event_type == "assistant":
        message = event.get("message", {})
        content_blocks = message.get("content", [])
        results: list[dict] = []
        for block in content_blocks:
            block_type = block.get("type", "")
            if block_type == "text":
                results.append({
                    "kind": "text",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "text": block.get("text", ""),
                })
            elif block_type == "thinking":
                results.append({
                    "kind": "thinking",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "text": block.get("thinking", ""),
                })
            elif block_type == "tool_use":
                results.append({
                    "kind": "tool_use",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "toolName": block.get("name", ""),
                    "toolCallId": block.get("id", _make_id()),
                    "toolInput": block.get("input", {}),
                })
            elif block_type == "tool_result":
                results.append({
                    "kind": "tool_result",
                    "id": _make_id(),
                    "sessionId": session_id,
                    "timestamp": _now(),
                    "toolCallId": block.get("tool_use_id", ""),
                    "toolResult": block.get("content", ""),
                    "isError": block.get("is_error", False),
                })
            else:
                logger.info("Skipping unknown content block type: %s", block_type)
        return results

    # --- content_block_delta: streaming partial text ---
    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type", "")
        if delta_type == "text_delta":
            return [{
                "kind": "stream_delta",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": delta.get("text", ""),
            }]
        if delta_type == "thinking_delta":
            return [{
                "kind": "stream_delta",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": delta.get("thinking", ""),
                "isThinking": True,
            }]
        return []

    # --- content_block_stop: end of streaming block ---
    if event_type == "content_block_stop":
        return [{
            "kind": "stream_end",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
        }]

    # --- result: final completion with cost/usage ---
    if event_type == "result":
        usage = event.get("usage", {})
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": "result",
            "subtype": "result",
            "result": event.get("result", ""),
            "isError": event.get("is_error", False),
            "durationMs": event.get("duration_ms"),
            "numTurns": event.get("num_turns"),
            "totalCostUsd": event.get("total_cost_usd"),
            "stopReason": event.get("stop_reason"),
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            },
        }]

    # --- rate_limit_event: informational, skip ---
    if event_type == "rate_limit_event":
        return []

    # --- Unknown event type: log and skip ---
    logger.warning("Unknown Claude CLI event type: %s", event_type)
    return []
