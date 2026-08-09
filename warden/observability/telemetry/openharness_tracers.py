"""Facade composing the OpenHarness Langfuse + OTEL tracers for one send()."""
from __future__ import annotations

from typing import Any


class OpenHarnessTracers:
    """Fans send()'s StreamEvents out to the Langfuse + OTEL tracers.

    Either leg may be ``None`` (Langfuse keys unset / opentelemetry absent); the
    facade guards both so ``session.py`` needs no per-tracer None checks.
    """

    def __init__(self, langfuse: Any, otel: Any):
        self._langfuse = langfuse
        self._otel = otel

    @classmethod
    def create(
        cls, session_id: str | None, prompt: str, model: str, cfg=None,
    ) -> "OpenHarnessTracers":
        from warden.observability.telemetry.openharness_langfuse_tracer import (
            OpenHarnessLangfuseTracer,
        )
        from warden.observability.telemetry.openharness_otel_tracer import (
            OpenHarnessOtelTracer,
        )
        return cls(
            OpenHarnessLangfuseTracer.create(session_id, prompt, model, cfg),
            OpenHarnessOtelTracer.create(model),
        )

    def set_final_output(self, text: str) -> None:
        if self._langfuse:
            self._langfuse.set_final_output(text)

    def handle_event(self, event: Any) -> None:
        if self._langfuse:
            self._langfuse.handle_event(event)
        if self._otel:
            self._otel.handle_event(event)

    def finalize(self) -> None:
        if self._langfuse:
            self._langfuse.register_agent_completion_listeners()
            self._langfuse.finalize()
        if self._otel:
            self._otel.finalize()
