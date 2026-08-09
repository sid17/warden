"""EXT-P1/A2 (E4) — the milestone/completion re-tag producer.

The runner re-tags a named custom-tool call (``kind=="tool_use"``) into a typed
egress event per the workflow's ``event_tool_map``, passing ``toolInput`` through
OPAQUE (no ``gen_ai.*`` bleed). Provider-agnostic (one site in the runner). Follows
the ``test_runs_api`` pattern: drive ``_handle_message`` directly with a fake egress.
"""

import asyncio

from warden.harness_api.runner import Runner, _RunState
from warden.schemas.events import MessageEvent


class _Egress:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    async def aclose(self):
        ...


def _drive(state, oe):
    runner = Runner()
    egress = _Egress()

    async def _go():
        await runner._ensure_event_log()
        await runner._handle_message("r", egress, state, oe)

    asyncio.run(_go())
    return egress.events


def _drive_seq(state, oes):
    """Drive a SEQUENCE of events on ONE state (so per-run flags persist)."""
    runner = Runner()
    egress = _Egress()

    async def _go():
        await runner._ensure_event_log()
        for oe in oes:
            await runner._handle_message("r", egress, state, oe)

    asyncio.run(_go())
    return egress.events


def _state(event_tool_map):
    st = _RunState(run_id="r", user_id="u", task_id="t")
    st.event_tool_map = event_tool_map
    return st


# --- N2: streaming must not ALSO emit the assembled TextBlock (no duplication) ---


def test_streaming_suppresses_duplicate_assembled_text_token():
    """N2 — with partial streaming, the Claude SDK emits the answer twice: once as
    ``stream_delta`` deltas, then again as the final assembled ``text`` TextBlock. Both
    map to ``token`` events → the answer is duplicated (the observed Q&A/Notes bug). Once
    a stream_delta has been seen, the trailing full text block is a duplicate → suppress."""
    st = _RunState(run_id="r", user_id="u", task_id="t")
    events = _drive_seq(st, [
        MessageEvent(kind="stream_delta", content={"text": "Hello"}),
        MessageEvent(kind="stream_delta", content={"text": " world"}),
        MessageEvent(kind="text", content={"text": "Hello world"}),  # assembled dup
    ])
    tokens = [e.data["text"] for e in events if e.type == "token"]
    assert tokens == ["Hello", " world"], f"duplicated: {tokens}"


def test_text_token_emitted_when_no_streaming():
    """A non-streaming run (no stream_delta) — the assembled text is the ONLY source, so
    it must still emit (providers/paths without include_partial_messages)."""
    st = _RunState(run_id="r", user_id="u", task_id="t")
    events = _drive_seq(st, [MessageEvent(kind="text", content={"text": "answer"})])
    assert [e.data["text"] for e in events if e.type == "token"] == ["answer"]


def test_checkpoint_retag_is_opaque():
    st = _state({"emit_checkpoint": "checkpoint"})
    oe = MessageEvent(kind="tool_use",
                      content={"toolName": "emit_checkpoint",
                               "toolInput": {"phase": "scout"}})
    (ev,) = _drive(st, oe)
    assert ev.type == "checkpoint"
    # opaque: the toolInput passes through verbatim, no gen_ai.* keys mixed in.
    assert ev.data == {"phase": "scout"}
    assert not any(k.startswith("gen_ai.") for k in ev.data)


def test_completion_retag_carries_files_manifest():
    st = _state({"course_complete": "completion"})
    files = [{"path": "ch1.md", "title": "One", "order": 1}]
    oe = MessageEvent(kind="tool_use",
                      content={"toolName": "course_complete",
                               "toolInput": {"files": files}})
    (ev,) = _drive(st, oe)
    assert ev.type == "completion"
    assert ev.data == {"files": files}


def test_fqmn_prefix_is_stripped_before_lookup():
    st = _state({"emit_checkpoint": "checkpoint"})
    # Claude delivers the fully-qualified MCP name; the bare name must still map.
    oe = MessageEvent(kind="tool_use",
                      content={"toolName": "mcp__harness_custom__emit_checkpoint",
                               "toolInput": {"phase": "index"}})
    (ev,) = _drive(st, oe)
    assert ev.type == "checkpoint"
    assert ev.data == {"phase": "index"}


def test_unmapped_tool_use_still_emits_as_tool_use():
    st = _state({"emit_checkpoint": "checkpoint"})
    oe = MessageEvent(kind="tool_use",
                      content={"toolName": "Bash", "toolInput": {}, "toolCallId": "x"})
    (ev,) = _drive(st, oe)
    assert ev.type == "tool_use"
    # the gen_ai.* semconv merge still applies to the non-mapped path (no regression)
    assert ev.data["gen_ai.tool.name"] == "Bash"
    assert ev.data["toolName"] == "Bash"


def test_empty_map_is_pure_passthrough():
    st = _state({})
    oe = MessageEvent(kind="tool_use",
                      content={"toolName": "emit_checkpoint", "toolInput": {}})
    (ev,) = _drive(st, oe)
    assert ev.type == "tool_use"  # no map ⇒ no re-tag
