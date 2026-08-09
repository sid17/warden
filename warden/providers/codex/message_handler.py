import time
import uuid


def _make_id() -> str:
    return str(uuid.uuid4())


def _now() -> float:
    return time.time()


def transform_codex_message(event: dict, session_id: str) -> list[dict]:
    """Convert a Codex CLI JSONL event into ChatMessage dicts.

    Real event format from `codex exec --json`:
      {"type":"thread.started","thread_id":"..."}
      {"type":"turn.started"}
      {"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"...","status":"in_progress"}}
      {"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"...","aggregated_output":"...","exit_code":0,"status":"completed"}}
      {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"..."}}
      {"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}
      {"type":"error","message":"..."}
    """
    event_type = event.get("type", "")

    # item.started — command execution in progress (show as tool_use)
    if event_type == "item.started":
        item = event.get("item", {})
        if item.get("type") == "command_execution":
            return [{
                "kind": "tool_use",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "toolName": "Bash",
                "toolCallId": item.get("id", _make_id()),
                "toolInput": {"command": item.get("command", "")},
            }]
        return []

    # item.completed — agent message or command result
    if event_type == "item.completed":
        item = event.get("item", {})
        item_type = item.get("type", "")

        # Agent text response
        if item_type == "agent_message":
            return [{
                "kind": "text",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": item.get("text", ""),
            }]

        # Command execution completed (show as tool_result)
        if item_type == "command_execution":
            return [{
                "kind": "tool_result",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "toolCallId": item.get("id", ""),
                "toolResult": item.get("aggregated_output", ""),
                "isError": (item.get("exit_code") or 0) != 0,
            }]

        # File change
        if item_type == "file_change":
            return [{
                "kind": "tool_use",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "toolName": "FileEdit",
                "toolCallId": item.get("id", _make_id()),
                "toolInput": {
                    "path": item.get("path", ""),
                    "diff": item.get("diff", ""),
                },
            }]

        # Reasoning
        if item_type == "reasoning":
            return [{
                "kind": "thinking",
                "id": _make_id(),
                "sessionId": session_id,
                "timestamp": _now(),
                "text": item.get("text", ""),
            }]

        return []

    # Error
    if event_type == "error":
        return [{
            "kind": "error",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": event.get("message", str(event)),
            "isError": True,
        }]

    # Turn completed — emit result status with usage
    if event_type == "turn.completed":
        usage = event.get("usage", {})
        return [{
            "kind": "status",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": "result",
            "subtype": "result",
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
            },
        }]

    # Turn failed
    if event_type == "turn.failed":
        error = event.get("error", {})
        return [{
            "kind": "error",
            "id": _make_id(),
            "sessionId": session_id,
            "timestamp": _now(),
            "text": error.get("message", "Turn failed"),
            "isError": True,
        }]

    # thread.started, turn.started — no user-visible output
    return []
