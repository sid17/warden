"""Hermetic unit tests for the OpenHarness OTEL turn + tool tracer.

No real OpenTelemetry / network. The tracer is duck-typed (``Any``) in the
source, so ``OpenHarnessOtelTracer`` is constructed DIRECTLY (bypassing
``.create()``) with a ``FakeTracer`` that records the span names it minted and
whose spans record ``set_attribute(k, v)`` + ``end()``. OpenHarness stream
events are faked with class-named ``SimpleNamespace`` shims (``handle_event``
dispatches on ``type(event).__name__``).
"""
from __future__ import annotations

from types import SimpleNamespace

from warden.observability.telemetry.openharness_otel_tracer import (
    OpenHarnessOtelTracer,
)
from warden.schemas import semconv


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes: dict = {}
        self.ended = False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self):
        self.ended = True


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def start_span(self, name: str) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


def _named(obj, name):
    """Return an object whose ``type(obj).__name__`` equals ``name``."""
    cls = type(name, (), {})
    inst = cls()
    for k, v in vars(obj).items():
        setattr(inst, k, v)
    return inst


def turn_complete(input_tokens=0, output_tokens=0, with_usage=True):
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return _named(SimpleNamespace(usage=usage if with_usage else None),
                  "AssistantTurnComplete")


def tool_started(tool_name="Bash", tool_input=None):
    return _named(
        SimpleNamespace(tool_name=tool_name, tool_input=tool_input or {}),
        "ToolExecutionStarted",
    )


def tool_completed(output="ok", is_error=False):
    return _named(
        SimpleNamespace(output=output, is_error=is_error),
        "ToolExecutionCompleted",
    )


def make_tracer(model="qwen3:8b"):
    fake = FakeTracer()
    return OpenHarnessOtelTracer(tracer=fake, model=model), fake


def _span_named(fake, name):
    for s in fake.spans:
        if s.name == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Turn spans
# ---------------------------------------------------------------------------

class TestTurnSpan:
    def test_turn_span_carries_usage_and_model(self):
        t, fake = make_tracer("qwen3:8b")
        t.handle_event(turn_complete(input_tokens=100, output_tokens=40))
        span = _span_named(fake, "turn 1")
        assert span is not None
        assert span.attributes[semconv.USAGE_INPUT_TOKENS] == 100
        assert span.attributes[semconv.USAGE_OUTPUT_TOKENS] == 40
        assert span.attributes[semconv.REQUEST_MODEL] == "qwen3:8b"
        assert span.ended is True

    def test_turn_count_increments(self):
        t, fake = make_tracer()
        t.handle_event(turn_complete(input_tokens=1, output_tokens=1))
        t.handle_event(turn_complete(input_tokens=2, output_tokens=2))
        assert _span_named(fake, "turn 1") is not None
        assert _span_named(fake, "turn 2") is not None

    def test_missing_usage_defaults_zero(self):
        t, fake = make_tracer()
        t.handle_event(turn_complete(with_usage=False))
        span = _span_named(fake, "turn 1")
        assert span.attributes[semconv.USAGE_INPUT_TOKENS] == 0
        assert span.attributes[semconv.USAGE_OUTPUT_TOKENS] == 0


# ---------------------------------------------------------------------------
# Tool spans
# ---------------------------------------------------------------------------

class TestToolSpan:
    def test_tool_started_then_completed(self):
        t, fake = make_tracer()
        t.handle_event(tool_started(tool_name="Bash", tool_input={}))
        t.handle_event(tool_completed(output="ok", is_error=False))
        span = _span_named(fake, "tool: Bash")
        assert span is not None
        assert span.attributes[semconv.TOOL_NAME] == "Bash"
        assert span.attributes[semconv.OPERATION_NAME] == "execute_tool"
        assert span.attributes["error"] is False
        assert span.ended is True

    def test_tool_error_flag_propagated(self):
        t, fake = make_tracer()
        t.handle_event(tool_started(tool_name="Bash"))
        t.handle_event(tool_completed(output="boom", is_error=True))
        span = _span_named(fake, "tool: Bash")
        assert span.attributes["error"] is True

    def test_completed_without_open_span_noop(self):
        t, fake = make_tracer()
        t.handle_event(tool_completed(output="x"))
        # No tool span created.
        assert _span_named(fake, "tool: Bash") is None


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------

class TestFinalize:
    def test_finalize_closes_dangling_tool_span(self):
        t, fake = make_tracer()
        t.handle_event(tool_started(tool_name="Bash"))
        span = _span_named(fake, "tool: Bash")
        assert span.ended is False
        t.finalize()
        assert span.ended is True

    def test_finalize_noop_when_nothing_open(self):
        t, fake = make_tracer()
        t.finalize()  # must not raise

    def test_finalize_force_flushes_provider(self, monkeypatch):
        # A short-lived run may exit before the BatchSpanProcessor tick; finalize
        # must force_flush so the turn/tool spans reach the collector.
        import opentelemetry.trace as ot

        flushed = {"called": False}

        class _Prov:
            def force_flush(self):
                flushed["called"] = True

        monkeypatch.setattr(ot, "get_tracer_provider", lambda: _Prov())
        t, _ = make_tracer()
        t.finalize()
        assert flushed["called"] is True

    def test_finalize_safe_when_provider_has_no_force_flush(self, monkeypatch):
        # The API's default no-op provider has no force_flush — must not raise.
        import opentelemetry.trace as ot

        monkeypatch.setattr(ot, "get_tracer_provider", lambda: object())
        t, _ = make_tracer()
        t.finalize()  # must not raise


# ---------------------------------------------------------------------------
# Facade fan-out
# ---------------------------------------------------------------------------

class TestFacadeFanOut:
    def test_handle_event_fans_out_to_both_legs(self):
        from unittest.mock import MagicMock

        from warden.observability.telemetry.openharness_tracers import (
            OpenHarnessTracers,
        )

        lf, otel = MagicMock(name="lf"), MagicMock(name="otel")
        facade = OpenHarnessTracers(langfuse=lf, otel=otel)
        ev = turn_complete(input_tokens=1, output_tokens=1)
        facade.handle_event(ev)
        lf.handle_event.assert_called_once_with(ev)
        otel.handle_event.assert_called_once_with(ev)

    def test_none_legs_are_guarded(self):
        from warden.observability.telemetry.openharness_tracers import (
            OpenHarnessTracers,
        )

        facade = OpenHarnessTracers(langfuse=None, otel=None)
        # None of these should raise.
        facade.set_final_output("x")
        facade.handle_event(turn_complete())
        facade.finalize()
