"""Tests for providers.claude.message_handler — ChatMessage dict output."""

from warden.providers.claude.message_handler import transform_sdk_message


SESSION_ID = "test-session-123"


class _FakeBlock:
    """Lightweight stand-in for SDK content blocks (type().__name__ is used)."""
    def __init__(self, class_name: str, **attrs):
        self.__class__ = type(class_name, (), {})
        for k, v in attrs.items():
            object.__setattr__(self, k, v)


def _make_msg_with_blocks(*blocks):
    """Create a fake SDK message with content blocks."""
    msg = _FakeBlock("AssistantMessage", content=list(blocks))
    return msg


def _assert_common_fields(result: dict, expected_kind: str):
    """Assert that common ChatMessage fields are present and correct."""
    assert result["kind"] == expected_kind
    assert isinstance(result["id"], str)
    assert len(result["id"]) > 0
    assert isinstance(result["timestamp"], (int, float))
    assert result["sessionId"] == SESSION_ID


def test_text_block():
    block = _FakeBlock("TextBlock", text="Hello world")
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "text")
    assert results[0]["text"] == "Hello world"


def test_thinking_block():
    block = _FakeBlock("ThinkingBlock", thinking="Let me think...")
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "thinking")
    assert results[0]["text"] == "Let me think..."


def test_tool_use_block():
    block = _FakeBlock(
        "ToolUseBlock",
        name="read_file",
        id="tool-call-1",
        input={"path": "/tmp/test.py"},
    )
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "tool_use")
    assert results[0]["toolName"] == "read_file"
    assert results[0]["toolCallId"] == "tool-call-1"
    assert results[0]["toolInput"] == {"path": "/tmp/test.py"}


def test_tool_result_block():
    block = _FakeBlock(
        "ToolResultBlock",
        tool_use_id="tool-call-1",
        content="file contents here",
        is_error=False,
    )
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "tool_result")
    assert results[0]["toolCallId"] == "tool-call-1"
    assert results[0]["toolResult"] == "file contents here"
    assert results[0]["isError"] is False


def test_error_message():
    msg = _FakeBlock("ErrorMessage", error="Something went wrong")
    # Must not have a list content attribute
    msg.content = None
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "error")
    assert results[0]["text"] == "Something went wrong"
    assert results[0]["isError"] is True


def test_unknown_block_returns_empty():
    block = _FakeBlock("UnknownBlockType")
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 0


def test_multiple_blocks():
    text_block = _FakeBlock("TextBlock", text="Hello")
    thinking_block = _FakeBlock("ThinkingBlock", thinking="Hmm")
    msg = _make_msg_with_blocks(text_block, thinking_block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 2
    assert results[0]["kind"] == "text"
    assert results[1]["kind"] == "thinking"


def test_result_message():
    from claude_agent_sdk import ResultMessage

    msg = ResultMessage(
        subtype="success",
        duration_ms=1500,
        duration_api_ms=1200,
        is_error=False,
        num_turns=3,
        session_id="sess-1",
        total_cost_usd=0.05,
    )
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "status")
    assert results[0]["subtype"] == "result"
    assert results[0]["durationMs"] == 1500
    assert results[0]["isError"] is False


def test_unhandled_message_returns_empty():
    msg = _FakeBlock("SomeRandomMessage")
    msg.content = None
    msg.error = None
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 0


# --- StreamEvent handling ---

from claude_agent_sdk import StreamEvent


def test_stream_event_text_delta():
    event = StreamEvent(
        uuid="evt-1",
        session_id="sess-1",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
    )
    results = transform_sdk_message(event, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "stream_delta")
    assert results[0]["text"] == "hello"


def test_stream_event_content_block_stop():
    event = StreamEvent(
        uuid="evt-2",
        session_id="sess-1",
        event={"type": "content_block_stop"},
    )
    results = transform_sdk_message(event, SESSION_ID)

    assert len(results) == 1
    _assert_common_fields(results[0], "stream_end")


def test_stream_event_other_type_skipped():
    event = StreamEvent(
        uuid="evt-3",
        session_id="sess-1",
        event={"type": "message_start"},
    )
    results = transform_sdk_message(event, SESSION_ID)

    assert len(results) == 0


def test_text_block_still_works_with_streaming():
    """Regression: full TextBlock messages still handled after adding StreamEvent support."""
    block = _FakeBlock("TextBlock", text="Full text")
    msg = _make_msg_with_blocks(block)
    results = transform_sdk_message(msg, SESSION_ID)

    assert len(results) == 1
    assert results[0]["kind"] == "text"
    assert results[0]["text"] == "Full text"
