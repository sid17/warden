"""Tests for providers.claude.cli_message_handler — NDJSON → ChatMessage dicts."""

from warden.providers.claude.cli_message_handler import transform_cli_message


SESSION_ID = "test-session-cli-123"

# --- Real fixtures based on captured claude -p output ---

SYSTEM_INIT = {
    "type": "system",
    "subtype": "init",
    "cwd": "/tmp",
    "session_id": "fc077302-ea96-42c0-8761-4882b8a19994",
    "tools": ["Bash", "Read", "Write"],
    "model": "claude-opus-4-6",
}

ASSISTANT_TEXT = {
    "type": "assistant",
    "message": {
        "model": "claude-opus-4-6",
        "id": "msg_01BxoYFqpXJ1ToFehWznnKL9",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello."}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    },
    "session_id": "fc077302-ea96-42c0-8761-4882b8a19994",
}

ASSISTANT_TOOL_USE = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "tool-call-abc",
                "name": "Bash",
                "input": {"command": "ls -la"},
            },
        ],
    },
    "session_id": "fc077302-ea96-42c0-8761-4882b8a19994",
}

ASSISTANT_THINKING = {
    "type": "assistant",
    "message": {
        "content": [
            {"type": "thinking", "thinking": "Let me think about this..."},
        ],
    },
    "session_id": "fc077302-ea96-42c0-8761-4882b8a19994",
}

CONTENT_BLOCK_TEXT_DELTA = {
    "type": "content_block_delta",
    "delta": {"type": "text_delta", "text": "Hello"},
}

CONTENT_BLOCK_THINKING_DELTA = {
    "type": "content_block_delta",
    "delta": {"type": "thinking_delta", "thinking": "Let me think..."},
}

CONTENT_BLOCK_STOP = {
    "type": "content_block_stop",
    "index": 0,
}

RESULT_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2125,
    "duration_api_ms": 2118,
    "num_turns": 1,
    "result": "Hello.",
    "stop_reason": "end_turn",
    "session_id": "fc077302-ea96-42c0-8761-4882b8a19994",
    "total_cost_usd": 0.05496775,
    "usage": {
        "input_tokens": 3,
        "cache_creation_input_tokens": 7901,
        "cache_read_input_tokens": 10893,
        "output_tokens": 5,
    },
}

RATE_LIMIT_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed"},
}

UNKNOWN_EVENT = {"type": "unknown_future_type", "data": {}}


# --- Helpers ---

def _assert_base_fields(result: dict, expected_kind: str):
    """Assert common ChatMessage fields are present."""
    assert result["kind"] == expected_kind
    assert isinstance(result["id"], str) and len(result["id"]) > 0
    assert isinstance(result["timestamp"], (int, float)) and result["timestamp"] > 0
    assert result["sessionId"] == SESSION_ID


# --- Tests ---

def test_system_init():
    results = transform_cli_message(SYSTEM_INIT, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "status")
    assert results[0]["subtype"] == "init"
    assert results[0]["model"] == "claude-opus-4-6"
    assert "Bash" in results[0]["tools"]


def test_system_non_init_returns_empty():
    event = {"type": "system", "subtype": "other_thing"}
    results = transform_cli_message(event, SESSION_ID)
    assert results == []


def test_assistant_text():
    results = transform_cli_message(ASSISTANT_TEXT, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "text")
    assert results[0]["text"] == "Hello."


def test_assistant_tool_use():
    results = transform_cli_message(ASSISTANT_TOOL_USE, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "tool_use")
    assert results[0]["toolName"] == "Bash"
    assert results[0]["toolCallId"] == "tool-call-abc"
    assert results[0]["toolInput"] == {"command": "ls -la"}


def test_assistant_thinking():
    results = transform_cli_message(ASSISTANT_THINKING, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "thinking")
    assert results[0]["text"] == "Let me think about this..."


def test_assistant_multiple_blocks():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "Hmm"},
                {"type": "text", "text": "Answer"},
            ],
        },
    }
    results = transform_cli_message(event, SESSION_ID)
    assert len(results) == 2
    assert results[0]["kind"] == "thinking"
    assert results[1]["kind"] == "text"


def test_text_delta():
    results = transform_cli_message(CONTENT_BLOCK_TEXT_DELTA, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "stream_delta")
    assert results[0]["text"] == "Hello"


def test_thinking_delta():
    results = transform_cli_message(CONTENT_BLOCK_THINKING_DELTA, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "stream_delta")
    assert results[0]["text"] == "Let me think..."
    assert results[0]["isThinking"] is True


def test_content_block_stop():
    results = transform_cli_message(CONTENT_BLOCK_STOP, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "stream_end")


def test_result_success():
    results = transform_cli_message(RESULT_SUCCESS, SESSION_ID)
    assert len(results) == 1
    _assert_base_fields(results[0], "status")
    assert results[0]["subtype"] == "result"
    assert results[0]["isError"] is False
    assert results[0]["durationMs"] == 2125
    assert results[0]["numTurns"] == 1
    assert results[0]["totalCostUsd"] == 0.05496775
    assert results[0]["stopReason"] == "end_turn"
    assert results[0]["usage"]["input_tokens"] == 3
    assert results[0]["usage"]["output_tokens"] == 5


def test_rate_limit_event_returns_empty():
    results = transform_cli_message(RATE_LIMIT_EVENT, SESSION_ID)
    assert results == []


def test_unknown_event_returns_empty():
    results = transform_cli_message(UNKNOWN_EVENT, SESSION_ID)
    assert results == []


def test_base_fields_present_on_all_event_types():
    """Every returned dict must have kind, id, sessionId, timestamp."""
    test_events = [
        SYSTEM_INIT,
        ASSISTANT_TEXT,
        CONTENT_BLOCK_TEXT_DELTA,
        CONTENT_BLOCK_STOP,
        RESULT_SUCCESS,
    ]
    for event in test_events:
        results = transform_cli_message(event, SESSION_ID)
        for r in results:
            assert "kind" in r, f"Missing 'kind' for event type {event['type']}"
            assert "id" in r, f"Missing 'id' for event type {event['type']}"
            assert "sessionId" in r, f"Missing 'sessionId' for event type {event['type']}"
            assert "timestamp" in r, f"Missing 'timestamp' for event type {event['type']}"
            # id is a valid UUID string
            import uuid
            uuid.UUID(r["id"])  # raises if invalid
            assert r["timestamp"] > 0


def test_assistant_empty_content():
    event = {"type": "assistant", "message": {"content": []}}
    results = transform_cli_message(event, SESSION_ID)
    assert results == []


def test_result_error():
    event = {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "",
        "usage": {},
    }
    results = transform_cli_message(event, SESSION_ID)
    assert len(results) == 1
    assert results[0]["isError"] is True
