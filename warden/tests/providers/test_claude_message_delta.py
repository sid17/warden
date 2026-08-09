"""M2 3c — Claude ``message_delta`` cumulative output-token capture (B20a).

The mid-turn cost tripwire (3e) needs a data source: the Claude SDK's
``message_delta`` stream frames carry a cumulative ``usage.output_tokens`` as the
turn generates. Today the message handler reads usage only from the terminal
``ResultMessage``; this wires the mid-stream frame into a normalized
``usage_delta`` signal the orchestrator threads to a ``mid_stream``
``governor.check()``.

Requires ``include_partial_messages=True`` (already set on the Claude session).
"""

from __future__ import annotations

from claude_agent_sdk import StreamEvent

from warden.providers.claude.message_handler import transform_sdk_message


def _delta(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u1", session_id="s1", event=event)


def test_message_delta_emits_cumulative_usage() -> None:
    out = transform_sdk_message(
        _delta({
            "type": "message_delta",
            "delta": {"stop_reason": None},
            "usage": {"output_tokens": 42},
        }),
        "s1",
    )
    assert len(out) == 1
    msg = out[0]
    assert msg["kind"] == "usage_delta"
    assert msg["usage"]["output_tokens"] == 42
    assert msg["sessionId"] == "s1"


def test_message_delta_without_usage_is_ignored() -> None:
    out = transform_sdk_message(
        _delta({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        "s1",
    )
    assert out == []


def test_text_delta_still_streams_unchanged() -> None:
    # The new branch must not regress the existing text streaming path.
    out = transform_sdk_message(
        _delta({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hi"},
        }),
        "s1",
    )
    assert len(out) == 1 and out[0]["kind"] == "stream_delta"
    assert out[0]["text"] == "hi"
