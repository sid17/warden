"""OTEL turn + tool spans for the OpenHarness parent loop.

``init_openharness_otel()`` configures an OpenAI instrumentor that captures
LLM-call spans only. This tracer adds the explicit **turn** and
**tool_use/tool_result** span tiers around ``QueryEngine.submit_message`` so the
OpenHarness OTEL trace has the same span taxonomy as Claude's native
``interaction → llm_request``, under the shared gen_ai.* semconv
(``schemas/semconv.py``).

Self-gating: ``trace.get_tracer()`` returns a no-op tracer when no
``TracerProvider`` is configured (telemetry off / ``init_openharness_otel``
skipped), so every span is a no-op with zero config plumbing here — M3 4b's
``enable_telemetry`` switch (which gates ``init_openharness_otel``) transitively
gates this tracer.
"""
from __future__ import annotations

import logging
from typing import Any

from warden.schemas import semconv

logger = logging.getLogger(__name__)

_TRACER_NAME = "warden.openharness"


class OpenHarnessOtelTracer:
    """Emits turn (marker) + tool (open/close) OTEL spans for one send()."""

    def __init__(self, tracer: Any, model: str):
        self._tracer = tracer
        self._model = model
        self._turn_count = 0
        self._open_tool_span: Any = None

    @classmethod
    def create(cls, model: str) -> "OpenHarnessOtelTracer | None":
        try:
            from opentelemetry import trace
        except Exception:
            return None
        return cls(trace.get_tracer(_TRACER_NAME), model)

    def handle_event(self, event: Any) -> None:
        etype = type(event).__name__
        if etype == "AssistantTurnComplete":
            self._turn(event)
        elif etype == "ToolExecutionStarted":
            self._tool_started(event)
        elif etype == "ToolExecutionCompleted":
            self._tool_completed(event)

    def _turn(self, event: Any) -> None:
        self._turn_count += 1
        usage = getattr(event, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        span = self._tracer.start_span(f"turn {self._turn_count}")
        for k, v in semconv.usage_attrs(
            {"input_tokens": in_tok, "output_tokens": out_tok}, model=self._model
        ).items():
            span.set_attribute(k, v)
        span.end()

    def _tool_started(self, event: Any) -> None:
        tool_name = getattr(event, "tool_name", "unknown")
        span = self._tracer.start_span(f"tool: {tool_name}")
        for k, v in semconv.tool_attrs(tool_name).items():
            span.set_attribute(k, v)
        self._open_tool_span = span

    def _tool_completed(self, event: Any) -> None:
        if not self._open_tool_span:
            return
        self._open_tool_span.set_attribute("error", bool(getattr(event, "is_error", False)))
        self._open_tool_span.end()
        self._open_tool_span = None

    def finalize(self) -> None:
        if self._open_tool_span:
            self._open_tool_span.end()
            self._open_tool_span = None
        # Export the batched spans now. A short-lived run often exits before the
        # BatchSpanProcessor's schedule tick and without a provider shutdown, so
        # without an explicit flush the turn/tool spans never reach the collector.
        try:
            from opentelemetry import trace

            flush = getattr(trace.get_tracer_provider(), "force_flush", None)
            if flush:
                flush()
        except Exception:
            logger.debug("OpenHarness OTel force_flush failed", exc_info=True)
