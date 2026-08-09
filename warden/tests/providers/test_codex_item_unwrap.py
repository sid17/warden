"""Codex SDK RootModel item-unwrap regression (live audit trail fix).

The Codex Python SDK wraps stream items in a pydantic RootModel: the concrete
item lives on ``payload.item.root`` (``CommandExecutionThreadItem`` /
``AgentMessageThreadItem`` / ``UserMessageThreadItem``). The old handler read
``.type`` off the WRAPPER (always ``None``) and dropped every command execution.
The fix unwraps via ``getattr(item, "root", item)`` and derives the kind from the
concrete CLASS NAME when no explicit ``.type`` field is present.

These tests define REAL classes (not SimpleNamespace) so class-name detection is
exercised.
"""

from __future__ import annotations

from warden.providers.codex.sdk_message_handler import notification_to_event


# --- fakes: class name drives detection ------------------------------------


class CommandExecutionThreadItem:
    def __init__(self, command, id="c1", aggregated_output="", exit_code=0):
        self.command = command
        self.id = id
        self.aggregated_output = aggregated_output
        self.exit_code = exit_code


class AgentMessageThreadItem:
    def __init__(self, text, id="m1"):
        self.text = text
        self.id = id


class _ThreadItem:  # the RootModel wrapper
    def __init__(self, root):
        self.root = root


class _Payload:
    def __init__(self, item):
        self.item = item


class _Notif:
    def __init__(self, method, payload):
        self.method = method
        self.payload = payload


# --- 1. command execution started → tool_use (the dropped bug) -------------


def test_command_execution_started_yields_tool_use():
    ev = notification_to_event(
        _Notif(
            "item/started",
            _Payload(_ThreadItem(CommandExecutionThreadItem("wc -l x", id="c1"))),
        ),
        "sess",
    )
    assert ev is not None
    assert ev["kind"] == "tool_use"
    assert ev["toolName"] == "Bash"
    assert ev["toolCallId"] == "c1"
    assert ev["toolInput"] == {"command": "wc -l x"}


# --- 2. command execution completed → tool_result -------------------------


def test_command_execution_completed_yields_tool_result():
    ev = notification_to_event(
        _Notif(
            "item/completed",
            _Payload(
                _ThreadItem(
                    CommandExecutionThreadItem(
                        "x", id="c1", aggregated_output="hello", exit_code=0
                    )
                )
            ),
        ),
        "sess",
    )
    assert ev is not None
    assert ev["kind"] == "tool_result"
    assert ev["toolCallId"] == "c1"
    assert ev["toolResult"] == "hello"
    assert ev["isError"] is False


def test_command_execution_nonzero_exit_is_error():
    ev = notification_to_event(
        _Notif(
            "item/completed",
            _Payload(
                _ThreadItem(
                    CommandExecutionThreadItem(
                        "x", id="c1", aggregated_output="boom", exit_code=1
                    )
                )
            ),
        ),
        "sess",
    )
    assert ev is not None
    assert ev["kind"] == "tool_result"
    assert ev["isError"] is True


# --- 3. agent message completed → text ------------------------------------


def test_agent_message_completed_yields_text():
    ev = notification_to_event(
        _Notif(
            "item/completed",
            _Payload(_ThreadItem(AgentMessageThreadItem("hi there"))),
        ),
        "sess",
    )
    assert ev is not None
    assert ev["kind"] == "text"
    assert ev["text"] == "hi there"


# --- 4. backward-compat: old SDK with no ``.root`` wrapper -----------------


def test_backward_compat_item_without_root_wrapper():
    # ``.item`` is DIRECTLY the concrete item (no _ThreadItem wrapper); the
    # ``getattr(item, "root", item)`` fallback must still resolve it.
    ev = notification_to_event(
        _Notif(
            "item/started",
            _Payload(CommandExecutionThreadItem("ls -la", id="c9")),
        ),
        "sess",
    )
    assert ev is not None
    assert ev["kind"] == "tool_use"
    assert ev["toolName"] == "Bash"
    assert ev["toolCallId"] == "c9"
    assert ev["toolInput"] == {"command": "ls -la"}


# --- 5. non-command item on started → None --------------------------------


def test_agent_message_started_yields_none():
    # Agent messages only surface on completion; started returns None for
    # non-command items.
    ev = notification_to_event(
        _Notif(
            "item/started",
            _Payload(_ThreadItem(AgentMessageThreadItem("partial"))),
        ),
        "sess",
    )
    assert ev is None
