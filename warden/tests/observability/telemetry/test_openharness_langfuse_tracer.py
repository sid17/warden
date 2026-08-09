"""Hermetic unit tests for the OpenHarness Langfuse tracer.

No real Langfuse / OTel / network. `trace` and `lf_client` are duck-typed
(``Any``) in the source, so they are faked with MagicMocks recording
``.trace()`` / ``.span()`` / ``.generation()`` / ``.end()`` / ``.update()`` /
``.flush()``. OpenHarness stream events are faked with ``SimpleNamespace``.

Async completion listeners are driven with ``asyncio.run(...)`` (this engine
does not use pytest-asyncio).
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from warden.observability.telemetry import (
    openharness_langfuse_tracer as tracer_mod,
)
from warden.observability.telemetry.openharness_langfuse_tracer import (
    OpenHarnessLangfuseTracer,
    _parse_agent_task_id,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fake_langfuse_model(monkeypatch):
    """Provide a stand-in ``langfuse.model.ModelUsage`` (langfuse not installed).

    ``_handle_turn_complete`` does ``from langfuse.model import ModelUsage``,
    so a fake module must live in ``sys.modules`` for token accumulation tests.
    """
    if "langfuse.model" in sys.modules:
        yield
        return
    pkg = types.ModuleType("langfuse")
    model_mod = types.ModuleType("langfuse.model")

    class ModelUsage:  # noqa: D401 - simple record
        def __init__(self, input=0, output=0, total=0, unit="TOKENS"):
            self.input = input
            self.output = output
            self.total = total
            self.unit = unit

    model_mod.ModelUsage = ModelUsage
    pkg.model = model_mod
    monkeypatch.setitem(sys.modules, "langfuse", pkg)
    monkeypatch.setitem(sys.modules, "langfuse.model", model_mod)
    yield


def make_tracer(model: str = "qwen3:1.7b"):
    """Build a tracer wired to fake trace + lf client mocks."""
    trace = MagicMock(name="trace")
    lf = MagicMock(name="lf_client")
    # Each .span()/.generation() call returns a fresh recording mock.
    trace.span.side_effect = lambda **kw: MagicMock(name="span")
    trace.generation.side_effect = lambda **kw: MagicMock(name="generation")
    return OpenHarnessLangfuseTracer(trace, lf, model), trace, lf


def turn_complete(input_tokens=0, output_tokens=0, with_usage=True):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    ev = SimpleNamespace(usage=usage if with_usage else None)
    return _named(ev, "AssistantTurnComplete")


def _named(obj, name):
    """Return an object whose ``type(obj).__name__`` equals ``name``.

    ``handle_event`` dispatches on the class name, so build a dedicated type.
    """
    cls = type(name, (), {})
    inst = cls()
    for k, v in vars(obj).items():
        setattr(inst, k, v)
    return inst


def tool_started(tool_name="read_file", tool_input=None):
    return _named(
        SimpleNamespace(tool_name=tool_name, tool_input=tool_input or {"path": "/x"}),
        "ToolExecutionStarted",
    )


def tool_completed(output="done", is_error=False):
    return _named(
        SimpleNamespace(output=output, is_error=is_error),
        "ToolExecutionCompleted",
    )


# ---------------------------------------------------------------------------
# _parse_agent_task_id (pure helper)
# ---------------------------------------------------------------------------

class TestParseAgentTaskId:
    def test_valid(self):
        out = "Spawned agent agent@default (task_id=a871a9eae, backend=subprocess)"
        assert _parse_agent_task_id(out) == "a871a9eae"

    def test_alphanumeric(self):
        assert _parse_agent_task_id("task_id=abc123XYZ end") == "abc123XYZ"

    def test_invalid_no_match(self):
        assert _parse_agent_task_id("no identifier here") is None

    def test_empty_string(self):
        assert _parse_agent_task_id("") is None


# ---------------------------------------------------------------------------
# create() classmethod
# ---------------------------------------------------------------------------

class TestCreate:
    def test_returns_none_when_langfuse_disabled(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "get_langfuse", lambda *a: None)
        result = OpenHarnessLangfuseTracer.create("sess-1", "hi", "model-x")
        assert result is None

    def test_builds_tracer_and_opens_trace(self, monkeypatch):
        lf = MagicMock(name="lf")
        monkeypatch.setattr(tracer_mod, "get_langfuse", lambda *a: lf)
        result = OpenHarnessLangfuseTracer.create("sess-1", "prompt text", "model-x")
        assert isinstance(result, OpenHarnessLangfuseTracer)
        lf.trace.assert_called_once()
        kwargs = lf.trace.call_args.kwargs
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["input"] == "prompt text"
        assert kwargs["metadata"]["model"] == "model-x"


# ---------------------------------------------------------------------------
# handle_event dispatch
# ---------------------------------------------------------------------------

class TestHandleEventDispatch:
    def test_turn_complete_routed(self):
        t, trace, _ = make_tracer()
        t.handle_event(turn_complete(input_tokens=1, output_tokens=2))
        trace.generation.assert_called_once()

    def test_tool_started_routed(self):
        t, trace, _ = make_tracer()
        t.handle_event(tool_started())
        trace.span.assert_called_once()

    def test_unknown_event_ignored(self):
        t, trace, _ = make_tracer()
        t.handle_event(_named(SimpleNamespace(), "SomethingElse"))
        trace.generation.assert_not_called()
        trace.span.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_turn_complete — token accumulation (README contract)
# ---------------------------------------------------------------------------

class TestTurnComplete:
    def test_usage_tokens_recorded(self):
        t, trace, _ = make_tracer()
        t.handle_event(turn_complete(input_tokens=10, output_tokens=5))
        kwargs = trace.generation.call_args.kwargs
        usage = kwargs["usage"]
        assert usage.input == 10
        assert usage.output == 5
        assert usage.total == 15
        assert usage.unit == "TOKENS"
        assert kwargs["name"] == "llm_call_1"
        assert kwargs["metadata"]["turn"] == 1

    def test_turn_count_increments(self):
        t, trace, _ = make_tracer()
        t.handle_event(turn_complete(input_tokens=1, output_tokens=1))
        t.handle_event(turn_complete(input_tokens=2, output_tokens=3))
        names = [c.kwargs["name"] for c in trace.generation.call_args_list]
        assert names == ["llm_call_1", "llm_call_2"]
        assert t._turn_count == 2

    def test_missing_usage_defaults_zero(self):
        t, trace, _ = make_tracer()
        t.handle_event(turn_complete(with_usage=False))
        usage = trace.generation.call_args.kwargs["usage"]
        assert usage.input == 0
        assert usage.output == 0
        assert usage.total == 0

    def test_final_output_used_as_generation_output(self):
        t, trace, _ = make_tracer()
        t.set_final_output("the answer")
        t.handle_event(turn_complete(input_tokens=1, output_tokens=1))
        assert trace.generation.call_args.kwargs["output"] == "the answer"
        # Empty output stays "" and is coerced to None on the generation.
        t2, trace2, _ = make_tracer()
        t2.set_final_output("")
        t2.handle_event(turn_complete(input_tokens=1, output_tokens=1))
        assert trace2.generation.call_args.kwargs["output"] is None


# ---------------------------------------------------------------------------
# tool span lifecycle
# ---------------------------------------------------------------------------

class TestToolSpanLifecycle:
    def test_started_opens_span(self):
        t, trace, _ = make_tracer()
        t.handle_event(tool_started(tool_name="grep", tool_input={"q": "x"}))
        kwargs = trace.span.call_args.kwargs
        assert kwargs["name"] == "tool: grep"
        assert kwargs["input"] == {"q": "x"}
        assert t._open_tool_span is not None

    def test_completed_ends_span(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        t, trace, _ = make_tracer()
        t.handle_event(tool_started(tool_name="grep"))
        span = t._open_tool_span
        t.handle_event(tool_completed(output="hits", is_error=False))
        span.end.assert_called_once()
        kwargs = span.end.call_args.kwargs
        assert kwargs["output"] == "hits"
        assert kwargs["metadata"]["is_error"] is False
        assert t._open_tool_span is None

    def test_completed_error_flag_propagated(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        t, _, _ = make_tracer()
        t.handle_event(tool_started(tool_name="bash"))
        span = t._open_tool_span
        t.handle_event(tool_completed(output="boom", is_error=True))
        assert span.end.call_args.kwargs["metadata"]["is_error"] is True

    def test_completed_without_open_span_noop(self):
        t, _, _ = make_tracer()
        # No open span — should return quietly.
        t.handle_event(tool_completed(output="x"))
        assert t._open_tool_span is None

    def test_agent_tool_holds_span_open(self):
        t, _, _ = make_tracer()
        t.handle_event(tool_started(tool_name="agent"))
        span = t._open_tool_span
        out = "Spawned agent agent@default (task_id=abc123, backend=subprocess)"
        t.handle_event(tool_completed(output=out))
        # Span held open (not ended) and stored under the task_id.
        span.end.assert_not_called()
        assert t._pending_agent_spans == {"abc123": span}
        assert t._open_tool_span is None

    def test_agent_tool_without_task_id_closes_normally(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        t, _, _ = make_tracer()
        t.handle_event(tool_started(tool_name="agent"))
        span = t._open_tool_span
        t.handle_event(tool_completed(output="no id in this output"))
        span.end.assert_called_once()
        assert t._pending_agent_spans == {}


# ---------------------------------------------------------------------------
# register_agent_completion_listeners — the risky closure logic
# ---------------------------------------------------------------------------

class FakeTaskRecord:
    def __init__(self, status="completed", return_code=0, created_at=100.0, ended_at=102.0):
        self.status = status
        self.return_code = return_code
        self.created_at = created_at
        self.ended_at = ended_at


class FakeTaskManager:
    """Minimal stand-in for BackgroundTaskManager.

    ``register_completion_listener`` records the async callback so a test can
    fire it manually; ``get_task`` returns whatever was seeded.
    """

    def __init__(self, tasks=None, outputs=None):
        self._tasks = tasks or {}
        self._outputs = outputs or {}
        self.listeners = []
        self.unregistered = []

    def get_task(self, task_id):
        return self._tasks.get(task_id)

    def read_task_output(self, task_id):
        return self._outputs.get(task_id, "")

    def register_completion_listener(self, cb):
        self.listeners.append(cb)

        def _unregister():
            self.unregistered.append(cb)

        return _unregister


def _patch_manager(monkeypatch, manager):
    """Patch the source-of-truth ``get_task_manager`` (imported inside method)."""
    import openharness.tasks.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "get_task_manager", lambda: manager)


class TestRegisterCompletionListeners:
    def test_no_pending_spans_is_noop(self, monkeypatch):
        # Even without patching, an empty pending dict returns immediately.
        t, _, _ = make_tracer()
        t.register_agent_completion_listeners()  # must not raise / import

    def test_already_completed_closes_immediately(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(
            tasks={"t1": FakeTaskRecord(status="completed", return_code=0)},
            outputs={"t1": "agent result"},
        )
        _patch_manager(monkeypatch, mgr)
        t, _, _ = make_tracer()
        span = MagicMock(name="agent_span")
        t._pending_agent_spans["t1"] = span

        t.register_agent_completion_listeners()

        # Fast-path: closed immediately, no listener registered.
        span.end.assert_called_once()
        end_kwargs = span.end.call_args.kwargs
        assert end_kwargs["output"] == "agent result"
        assert end_kwargs["metadata"]["status"] == "completed"
        assert end_kwargs["metadata"]["return_code"] == 0
        assert end_kwargs["metadata"]["duration_ms"] == 2000  # (102-100)*1000
        assert mgr.listeners == []
        assert "t1" not in t._pending_agent_spans

    def test_pending_registers_listener_and_firing_closes_span(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(
            tasks={"t2": FakeTaskRecord(status="running")},
            outputs={"t2": "final agent output"},
        )
        _patch_manager(monkeypatch, mgr)
        t, _, lf = make_tracer()
        span = MagicMock(name="agent_span")
        t._pending_agent_spans["t2"] = span

        t.register_agent_completion_listeners()

        # Running task → listener registered, span NOT yet closed.
        assert len(mgr.listeners) == 1
        span.end.assert_not_called()
        assert len(t._unregister_fns) == 1

        # Fire the completion callback (as the task manager would on finish).
        finished = FakeTaskRecord(
            status="completed", return_code=0, created_at=10.0, ended_at=13.5
        )
        asyncio.run(mgr.listeners[0](finished))

        span.end.assert_called_once()
        end_kwargs = span.end.call_args.kwargs
        assert end_kwargs["output"] == "final agent output"
        assert end_kwargs["metadata"]["status"] == "completed"
        assert end_kwargs["metadata"]["duration_ms"] == 3500  # (13.5-10)*1000
        lf.flush.assert_called_once()

    def test_listener_closure_binds_correct_span_per_task(self, monkeypatch):
        """Two pending agents → each listener must close ITS OWN span."""
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(
            tasks={
                "a": FakeTaskRecord(status="running"),
                "b": FakeTaskRecord(status="running"),
            },
            outputs={"a": "out-a", "b": "out-b"},
        )
        _patch_manager(monkeypatch, mgr)
        t, _, _ = make_tracer()
        span_a, span_b = MagicMock(name="span_a"), MagicMock(name="span_b")
        t._pending_agent_spans["a"] = span_a
        t._pending_agent_spans["b"] = span_b

        t.register_agent_completion_listeners()
        assert len(mgr.listeners) == 2

        # Fire only the SECOND listener; it must close span_b with out-b.
        asyncio.run(mgr.listeners[1](FakeTaskRecord(status="completed")))
        span_b.end.assert_called_once()
        assert span_b.end.call_args.kwargs["output"] == "out-b"
        span_a.end.assert_not_called()

    def test_listener_swallows_errors(self, monkeypatch):
        """Listener must never raise (span.end blows up → logged, not raised)."""
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(
            tasks={"t3": FakeTaskRecord(status="running")},
            outputs={"t3": "x"},
        )
        _patch_manager(monkeypatch, mgr)
        t, _, _ = make_tracer()
        span = MagicMock(name="span")
        span.end.side_effect = RuntimeError("langfuse down")
        t._pending_agent_spans["t3"] = span

        t.register_agent_completion_listeners()
        # Should not raise despite span.end throwing.
        asyncio.run(mgr.listeners[0](FakeTaskRecord(status="completed")))


# ---------------------------------------------------------------------------
# _close_all_pending
# ---------------------------------------------------------------------------

class TestCloseAllPending:
    def test_closes_every_span_with_fallback(self):
        t, _, _ = make_tracer()
        s1, s2 = MagicMock(name="s1"), MagicMock(name="s2")
        t._pending_agent_spans["x"] = s1
        t._pending_agent_spans["y"] = s2

        t._close_all_pending("(fallback)")

        for s in (s1, s2):
            s.end.assert_called_once_with(
                output="(fallback)",
                metadata={"status": "unknown", "internals_captured": False},
            )
        assert t._pending_agent_spans == {}

    def test_marks_internals_not_captured(self):
        # OBS-3: sub-agent internals run in an OpenHarness-owned child
        # subprocess we can't instrument — the boundary span must carry the
        # explicit marker so an empty span isn't read as "nothing happened".
        t, _, _ = make_tracer()
        span = MagicMock(name="span")
        t._pending_agent_spans["t1"] = span

        t._close_all_pending("fallback")

        assert span.end.call_args.kwargs["metadata"]["internals_captured"] is False


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------

class TestFinalize:
    def test_updates_trace_and_flushes(self):
        t, trace, lf = make_tracer("model-z")
        t.set_final_output("final text")
        t._turn_count = 3
        t.finalize()
        trace.update.assert_called_once()
        kwargs = trace.update.call_args.kwargs
        assert kwargs["output"] == "final text"
        assert kwargs["metadata"]["llm_calls"] == 3
        assert kwargs["metadata"]["model"] == "model-z"
        lf.flush.assert_called_once()

    def test_closes_open_tool_span(self):
        t, trace, _ = make_tracer()
        t.handle_event(tool_started(tool_name="grep"))
        span = t._open_tool_span
        t.finalize()
        span.end.assert_called_once_with(output="(no result received)")
        assert t._open_tool_span is None

    def test_pending_agent_span_completed_closed_via_task(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(
            tasks={"done": FakeTaskRecord(status="completed", return_code=0)},
            outputs={"done": "result"},
        )
        _patch_manager(monkeypatch, mgr)
        t, _, _ = make_tracer()
        span = MagicMock(name="span")
        t._pending_agent_spans["done"] = span

        t.finalize()

        span.end.assert_called_once()
        assert span.end.call_args.kwargs["metadata"]["status"] == "completed"
        # OBS-3 boundary marker lands on the completed-via-task close path too.
        assert span.end.call_args.kwargs["metadata"]["internals_captured"] is False

    def test_pending_agent_span_still_running_closed_as_running(self, monkeypatch):
        monkeypatch.setattr(tracer_mod, "truncate_output", lambda x: x)
        mgr = FakeTaskManager(tasks={"run": FakeTaskRecord(status="running")})
        _patch_manager(monkeypatch, mgr)
        t, _, _ = make_tracer()
        span = MagicMock(name="span")
        t._pending_agent_spans["run"] = span

        t.finalize()

        span.end.assert_called_once_with(
            output="(agent still running)",
            metadata={"status": "running", "internals_captured": False},
        )
        assert t._pending_agent_spans == {}

    def test_empty_final_output_becomes_none(self):
        t, trace, _ = make_tracer()
        t.finalize()
        assert trace.update.call_args.kwargs["output"] is None
