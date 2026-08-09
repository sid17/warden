"""Hermetic unit tests: GenAI semconv attrs on Langfuse generation records.

Both Langfuse tracers must attach the SHARED ``gen_ai.*`` attributes (from
``warden/schemas/semconv.py``) onto the ``metadata=`` dict of the
``generation(...)`` records they emit, so the Langfuse path speaks the same
vocabulary as the wire ``Event`` and the OTEL path.

No real Langfuse / network. ``trace`` / ``lf_client`` are duck-typed, faked
with objects/MagicMocks recording ``.generation(**kwargs)``. ``langfuse`` is
installed, so the lazy ``from langfuse.model import ModelUsage`` succeeds.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from warden.observability.telemetry.claude_langfuse_tracer import (
    ClaudeLangfuseTracer,
)
from warden.observability.telemetry.openharness_langfuse_tracer import (
    OpenHarnessLangfuseTracer,
)


class TestOpenHarnessSemconv:
    def test_turn_complete_metadata_carries_gen_ai_attrs(self):
        trace = MagicMock(name="trace")
        recorded: list[dict] = []
        trace.generation.side_effect = lambda **kw: recorded.append(kw)
        lf = MagicMock(name="lf")

        tracer = OpenHarnessLangfuseTracer(trace=trace, lf_client=lf, model="qwen3:8b")
        event = SimpleNamespace(
            usage=SimpleNamespace(input_tokens=100, output_tokens=40)
        )
        tracer._handle_turn_complete(event)

        assert len(recorded) == 1
        meta = recorded[0]["metadata"]
        assert meta["gen_ai.usage.input_tokens"] == 100
        assert meta["gen_ai.usage.output_tokens"] == 40
        assert meta["gen_ai.request.model"] == "qwen3:8b"
        # Existing key preserved.
        assert meta["turn"] == 1


class TestClaudeSemconv:
    def test_create_generation_metadata_carries_gen_ai_attrs(self):
        trace = MagicMock(name="trace")
        lf = MagicMock(name="lf")
        tracer = ClaudeLangfuseTracer(trace=trace, lf_client=lf)

        obs_parent = MagicMock(name="obs_parent")
        recorded: list[dict] = []
        obs_parent.generation.side_effect = lambda **kw: recorded.append(kw)

        msg = SimpleNamespace(stop_reason=None, message_id=None)
        tracer._create_generation(
            obs_parent=obs_parent,
            msg=msg,
            model="claude-opus-4-8",
            content=[],
            call_input=100,
            call_output=40,
            output_parts=[],
            parent_id=None,
        )

        assert len(recorded) == 1
        meta = recorded[0]["metadata"]
        assert meta["gen_ai.request.model"] == "claude-opus-4-8"
        assert meta["gen_ai.usage.input_tokens"] == 100
        assert meta["gen_ai.usage.output_tokens"] == 40
        assert meta["gen_ai.system"] == "anthropic"
        # Existing key preserved.
        assert "content_blocks" in meta
