"""T9 — hermetic OTEL taxonomy-parity lock (OBS-4).

This is the HERMETIC half of T9. Earlier M3 rungs made the GenAI-semconv
vocabulary uniform across every provider telemetry path by routing all of them
through ``warden/schemas/semconv.py``. This test LOCKS that
uniformity: every provider telemetry path emits the SAME ``gen_ai.*`` taxonomy
for usage/turn records and for tool records, and the OpenHarness sub-agent
boundary carries its ``internals_captured`` marker.

Four provider paths are driven with the exact fakes/drivers established in the
sibling tests (``test_runs_api.py``, ``test_tracer_semconv.py``,
``test_openharness_otel_tracer.py``, ``test_openharness_langfuse_tracer.py``):

- **Path A** — the wire ``Event`` (``Runner._handle_message`` / ``_emit_terminal``)
- **Path B** — Claude Langfuse ``generation`` metadata
- **Path C** — OpenHarness Langfuse ``generation`` metadata
- **Path D** — OpenHarness OTEL turn + tool spans

The parity assertions (bottom of the file) require the USAGE-record key sets
(Paths B/C/D-turn) and the TOOL-record key sets (Paths A/D-tool) to be EQUAL
across providers over the canonical keys — that equality is what makes this a
*parity* lock rather than N independent presence checks.

The full cross-provider LIVE parity (Claude's *native* OTEL export vs
OpenHarness) is asserted by the ``--telemetry-trace`` bed gate against a running
stack (a separate rung). Claude's native OTEL export happens inside a
subprocess, so it can only be observed live — it is deliberately NOT asserted
here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from warden.observability.telemetry.claude_langfuse_tracer import (
    ClaudeLangfuseTracer,
)
from warden.observability.telemetry.openharness_langfuse_tracer import (
    OpenHarnessLangfuseTracer,
)
from warden.observability.telemetry.openharness_otel_tracer import (
    OpenHarnessOtelTracer,
)
from warden.schemas import semconv

# --- The canonical taxonomy (imported constants, never hardcoded strings) ---

USAGE_KEYS = frozenset(
    {
        semconv.REQUEST_MODEL,
        semconv.USAGE_INPUT_TOKENS,
        semconv.USAGE_OUTPUT_TOKENS,
        semconv.OPERATION_NAME,
    }
)
TOOL_KEYS = frozenset({semconv.TOOL_NAME, semconv.OPERATION_NAME})


# ---------------------------------------------------------------------------
# Fakes mirrored from the sibling telemetry tests
# ---------------------------------------------------------------------------

class _FakeSpan:
    """OTEL span recording ``set_attribute`` + ``end`` (see the otel tracer test)."""

    def __init__(self, name: str):
        self.name = name
        self.attributes: dict = {}
        self.ended = False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self):
        self.ended = True


class _FakeTracer:
    def __init__(self):
        self.spans: list[_FakeSpan] = []

    def start_span(self, name: str) -> _FakeSpan:
        span = _FakeSpan(name)
        self.spans.append(span)
        return span


def _named(obj, name):
    """Object whose ``type(obj).__name__`` equals ``name`` (dispatch key)."""
    cls = type(name, (), {})
    inst = cls()
    for k, v in vars(obj).items():
        setattr(inst, k, v)
    return inst


def _turn_complete(input_tokens=10, output_tokens=5):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return _named(SimpleNamespace(usage=usage), "AssistantTurnComplete")


def _tool_started(tool_name="Bash", tool_input=None):
    return _named(
        SimpleNamespace(tool_name=tool_name, tool_input=tool_input or {}),
        "ToolExecutionStarted",
    )


def _tool_completed(output="ok", is_error=False):
    return _named(
        SimpleNamespace(output=output, is_error=is_error),
        "ToolExecutionCompleted",
    )


def _span_named(fake, name):
    for s in fake.spans:
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Path drivers — each returns the emitted key set for its taxonomy record
# ---------------------------------------------------------------------------

def _path_a_tool_keys() -> set:
    """Wire Event: drive a ``tool_use`` MessageEvent through the Runner."""
    from warden.harness_api.runner import Runner, _RunState
    from warden.schemas.events import MessageEvent

    runner = Runner()
    state = _RunState(run_id="r", user_id="u", task_id="t")
    oe = MessageEvent(
        kind="tool_use",
        content={"toolName": "Bash", "toolInput": {}, "toolCallId": "x"},
    )

    class _Egress:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

        async def aclose(self): ...

    egress = _Egress()

    async def _drive():
        await runner._ensure_event_log()
        await runner._handle_message("r", egress, state, oe)

    asyncio.run(_drive())
    (ev,) = egress.events
    assert ev.type == "tool_use"
    return set(ev.data)


def _path_a_usage_keys() -> set:
    """Wire Event: drive ``_emit_terminal`` to the terminal ``result`` event."""
    from warden.harness_api.runner import Runner, _RunState

    runner = Runner()
    state = _RunState(run_id="r", user_id="u", task_id="t", model="claude-opus-4-8")
    state.usage = {"input": 10, "output": 5, "cached": 0, "cost_usd": 0.1}
    state.result_text = "done"

    class _Egress:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

        async def aclose(self): ...

    egress = _Egress()

    async def _drive():
        await runner._ensure_event_log()
        await runner._emit_terminal("r", egress, state)

    asyncio.run(_drive())
    (ev,) = egress.events
    assert ev.type == "result"
    return set(ev.data)


def _path_b_usage_keys() -> set:
    """Claude Langfuse: ``_create_generation`` metadata keys."""
    trace = MagicMock(name="trace")
    lf = MagicMock(name="lf")
    tracer = ClaudeLangfuseTracer(trace=trace, lf_client=lf)

    obs_parent = MagicMock(name="obs_parent")
    recorded: list[dict] = []
    obs_parent.generation.side_effect = lambda **kw: recorded.append(kw)

    tracer._create_generation(
        obs_parent=obs_parent,
        msg=SimpleNamespace(stop_reason=None, message_id=None),
        model="claude-opus-4-8",
        content=[],
        call_input=10,
        call_output=5,
        output_parts=[],
        parent_id=None,
    )
    (rec,) = recorded
    return set(rec["metadata"])


def _path_c_usage_keys() -> set:
    """OpenHarness Langfuse: ``_handle_turn_complete`` metadata keys."""
    trace = MagicMock(name="trace")
    recorded: list[dict] = []
    trace.generation.side_effect = lambda **kw: recorded.append(kw)
    lf = MagicMock(name="lf")

    tracer = OpenHarnessLangfuseTracer(trace=trace, lf_client=lf, model="qwen3:8b")
    tracer._handle_turn_complete(_turn_complete(input_tokens=10, output_tokens=5))
    (rec,) = recorded
    return set(rec["metadata"])


def _path_d_usage_keys() -> set:
    """OpenHarness OTEL: ``turn 1`` span attribute keys."""
    fake = _FakeTracer()
    tracer = OpenHarnessOtelTracer(tracer=fake, model="qwen3:8b")
    tracer.handle_event(_turn_complete(input_tokens=10, output_tokens=5))
    span = _span_named(fake, "turn 1")
    assert span is not None
    return set(span.attributes)


def _path_d_tool_keys() -> set:
    """OpenHarness OTEL: ``tool: Bash`` span attribute keys."""
    fake = _FakeTracer()
    tracer = OpenHarnessOtelTracer(tracer=fake, model="qwen3:8b")
    tracer.handle_event(_tool_started(tool_name="Bash", tool_input={}))
    tracer.handle_event(_tool_completed(output="ok", is_error=False))
    span = _span_named(fake, "tool: Bash")
    assert span is not None
    return set(span.attributes)


# ---------------------------------------------------------------------------
# Per-path presence: emitted keys ⊇ the relevant canonical set
# ---------------------------------------------------------------------------

def test_path_a_tool_event_superset_of_tool_keys():
    assert _path_a_tool_keys() >= TOOL_KEYS


def test_path_a_result_event_superset_of_usage_keys():
    assert _path_a_usage_keys() >= USAGE_KEYS


def test_path_b_claude_generation_superset_of_usage_keys():
    assert _path_b_usage_keys() >= USAGE_KEYS


def test_path_c_openharness_generation_superset_of_usage_keys():
    assert _path_c_usage_keys() >= USAGE_KEYS


def test_path_d_openharness_turn_span_superset_of_usage_keys():
    assert _path_d_usage_keys() >= USAGE_KEYS


def test_path_d_openharness_tool_span_superset_of_tool_keys():
    assert _path_d_tool_keys() >= TOOL_KEYS


# ---------------------------------------------------------------------------
# Sub-agent boundary parity (OBS-3)
# ---------------------------------------------------------------------------

def test_openharness_subagent_boundary_marks_internals_not_captured():
    """OpenHarness sub-agents run in an OH-owned subprocess we can't instrument;
    the boundary span must carry ``internals_captured=False`` so an empty span
    is not misread as "nothing happened"."""
    tracer = OpenHarnessLangfuseTracer(
        trace=MagicMock(name="trace"),
        lf_client=MagicMock(name="lf"),
        model="qwen3:8b",
    )
    span = MagicMock(name="agent_span")
    tracer._pending_agent_spans["x"] = span

    tracer._close_all_pending("(fallback)")

    assert span.end.call_args.kwargs["metadata"]["internals_captured"] is False


def test_claude_opens_subagent_boundary_span():
    """Claude is the native-nesting reference: an ``Agent`` tool block opens a
    ``tool: Agent`` boundary span (keyed by the tool_use id) that sub-agent
    messages then nest under. We assert only that the boundary anchor is
    created — not deep nesting."""
    trace = MagicMock(name="trace")
    tracer = ClaudeLangfuseTracer(trace=trace, lf_client=MagicMock(name="lf"))

    obs_parent = MagicMock(name="obs_parent")
    obs_parent.span.side_effect = lambda **kw: MagicMock(name="agent_span")

    tracer._open_tool_spans_for_blocks(
        obs_parent=obs_parent,
        tool_blocks=[
            SimpleNamespace(
                id="a1",
                name="Agent",
                input={"subagent_type": "Explore", "description": "d"},
            )
        ],
        parent_id=None,
    )

    assert "a1" in tracer._agent_spans
    assert obs_parent.span.call_args.kwargs["name"] == "tool: Agent"


# ---------------------------------------------------------------------------
# The point of T9: cross-provider PARITY (equality, not just supersets)
# ---------------------------------------------------------------------------

def test_usage_taxonomy_is_identical_across_providers():
    """Paths B (Claude LF), C (OpenHarness LF) and D-turn (OpenHarness OTEL)
    must emit the EXACT SAME canonical usage taxonomy — the four
    ``gen_ai.*`` usage keys, no more no less on the parity slice."""
    b = _path_b_usage_keys() & USAGE_KEYS
    c = _path_c_usage_keys() & USAGE_KEYS
    d = _path_d_usage_keys() & USAGE_KEYS
    assert b == c == d == USAGE_KEYS


def test_tool_taxonomy_is_identical_across_providers():
    """Path A-tool (wire Event) and Path D-tool (OpenHarness OTEL) must emit the
    EXACT SAME canonical tool taxonomy on the parity slice."""
    a = _path_a_tool_keys() & TOOL_KEYS
    d = _path_d_tool_keys() & TOOL_KEYS
    assert a == d == TOOL_KEYS
