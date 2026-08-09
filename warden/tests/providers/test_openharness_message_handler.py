"""Tests for the OpenHarness message handler."""

from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    UsageSnapshot,
)
from openharness.engine.messages import ConversationMessage

from warden.providers.openharness.message_handler import (
    transform_openharness_message,
)

SESSION_ID = "test-session-123"


def _assert_common_fields(msg: dict, kind: str) -> None:
    """Assert that common fields are present and correct."""
    assert msg["kind"] == kind
    assert msg["sessionId"] == SESSION_ID
    assert isinstance(msg["id"], str) and len(msg["id"]) > 0
    assert isinstance(msg["timestamp"], float)


class TestAssistantTextDelta:
    def test_maps_to_stream_delta(self):
        event = AssistantTextDelta(text="Hello, world!")
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "stream_delta")
        assert result[0]["text"] == "Hello, world!"

    def test_empty_text(self):
        event = AssistantTextDelta(text="")
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        assert result[0]["text"] == ""


class TestToolExecutionStarted:
    def test_maps_to_tool_use(self):
        event = ToolExecutionStarted(
            tool_name="Bash",
            tool_input={"command": "ls -la"},
        )
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "tool_use")
        assert result[0]["toolName"] == "Bash"
        assert result[0]["toolInput"] == {"command": "ls -la"}
        assert isinstance(result[0]["toolCallId"], str)


class TestToolExecutionCompleted:
    def test_maps_to_tool_result_success(self):
        # First emit a started event to register the toolCallId
        started = ToolExecutionStarted(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
        )
        started_result = transform_openharness_message(started, SESSION_ID)
        tool_call_id = started_result[0]["toolCallId"]

        event = ToolExecutionCompleted(
            tool_name="Bash",
            output="hi\n",
            is_error=False,
        )
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "tool_result")
        assert result[0]["toolCallId"] == tool_call_id
        assert result[0]["toolResult"] == "hi\n"
        assert result[0]["isError"] is False

    def test_maps_to_tool_result_error(self):
        event = ToolExecutionCompleted(
            tool_name="FileRead",
            output="Permission denied",
            is_error=True,
        )
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "tool_result")
        assert result[0]["isError"] is True
        assert result[0]["toolResult"] == "Permission denied"


class TestConcurrentSessions:
    """Two sessions must not share pending tool-call ids.

    The Runner allows N concurrent runs. If the pending-id map is a process
    global keyed only by tool_name, two sessions interleaving start/complete
    for the same tool collide and link a tool_result to the WRONG session's id.
    """

    def test_interleaved_sessions_same_tool_link_own_id(self):
        session_a = "session-a"
        session_b = "session-b"

        started_a = transform_openharness_message(
            ToolExecutionStarted(tool_name="Bash", tool_input={"command": "a"}),
            session_a,
        )
        id_a = started_a[0]["toolCallId"]

        # Session B starts the SAME tool before A completes — this is what
        # overwrites the shared entry when keyed only by tool_name.
        started_b = transform_openharness_message(
            ToolExecutionStarted(tool_name="Bash", tool_input={"command": "b"}),
            session_b,
        )
        id_b = started_b[0]["toolCallId"]

        assert id_a != id_b

        # Each session completes; each result must link to its OWN start id.
        completed_a = transform_openharness_message(
            ToolExecutionCompleted(tool_name="Bash", output="out-a", is_error=False),
            session_a,
        )
        completed_b = transform_openharness_message(
            ToolExecutionCompleted(tool_name="Bash", output="out-b", is_error=False),
            session_b,
        )

        assert completed_a[0]["toolCallId"] == id_a
        assert completed_a[0]["sessionId"] == session_a
        assert completed_b[0]["toolCallId"] == id_b
        assert completed_b[0]["sessionId"] == session_b


class TestAssistantTurnComplete:
    def test_maps_to_status_result(self):
        message = ConversationMessage.from_user_text("test")
        usage = UsageSnapshot(input_tokens=100, output_tokens=50)
        event = AssistantTurnComplete(message=message, usage=usage)
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "status")
        assert result[0]["subtype"] == "result"
        assert result[0]["usage"]["input_tokens"] == 100
        assert result[0]["usage"]["output_tokens"] == 50


class TestErrorEvent:
    def test_maps_to_error(self):
        event = ErrorEvent(message="Something went wrong", recoverable=True)
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "error")
        assert result[0]["text"] == "Something went wrong"
        assert result[0]["isError"] is True


class TestStatusEvent:
    def test_maps_to_status(self):
        event = StatusEvent(message="Thinking...")
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "status")
        assert result[0]["text"] == "Thinking..."


class TestCompactProgressEvent:
    def test_maps_to_status_with_message(self):
        event = CompactProgressEvent(
            phase="compact_start",
            trigger="auto",
            message="Starting compaction",
        )
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        _assert_common_fields(result[0], "status")
        assert result[0]["text"] == "Starting compaction"

    def test_maps_to_status_without_message(self):
        event = CompactProgressEvent(
            phase="compact_end",
            trigger="manual",
        )
        result = transform_openharness_message(event, SESSION_ID)

        assert len(result) == 1
        assert result[0]["text"] == "compaction: compact_end"


class TestUnknownEvent:
    def test_returns_empty_list(self):
        result = transform_openharness_message("unknown_event", SESSION_ID)
        assert result == []

    def test_returns_empty_for_dict(self):
        result = transform_openharness_message({"type": "unknown"}, SESSION_ID)
        assert result == []
