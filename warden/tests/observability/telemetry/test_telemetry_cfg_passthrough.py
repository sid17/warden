"""M3 rung 4a: the TelemetryConfig slice is threaded down to the tracers and
the provider session ctors read the PASSED slice, not the global surface.

- Tracer ``create`` classmethods pass their ``cfg`` into ``get_langfuse``.
- Claude / OpenHarness session ctors accept a ``telemetry`` kwarg (without
  tripping the unknown-kwarg guard) and store it on ``self._telemetry``.

Sessions are constructed only (never ``.start()``ed) so no live SDK / Ollama.
"""

from __future__ import annotations

from pathlib import Path

from warden.config.models import TelemetryConfig

SENTINEL = TelemetryConfig(otel_collector_endpoint="http://sentinel:4317")
REPO = Path("/tmp")


def test_claude_langfuse_tracer_passes_cfg_to_get_langfuse(monkeypatch):
    import warden.observability.telemetry.claude_langfuse_tracer as mod

    seen: dict = {}

    def _fake_get_langfuse(cfg=None):
        seen["cfg"] = cfg
        return None  # falsy → create returns None; we only assert the arg

    monkeypatch.setattr(mod, "get_langfuse", _fake_get_langfuse)
    mod.ClaudeLangfuseTracer.create("s", "p", cfg=SENTINEL)
    assert seen["cfg"] is SENTINEL


def test_openharness_langfuse_tracer_passes_cfg_to_get_langfuse(monkeypatch):
    import warden.observability.telemetry.openharness_langfuse_tracer as mod

    seen: dict = {}

    def _fake_get_langfuse(cfg=None):
        seen["cfg"] = cfg
        return None

    monkeypatch.setattr(mod, "get_langfuse", _fake_get_langfuse)
    mod.OpenHarnessLangfuseTracer.create("s", "p", "m", cfg=SENTINEL)
    assert seen["cfg"] is SENTINEL


def test_claude_session_accepts_telemetry_kwarg():
    from warden.providers.claude.session import ClaudeSession

    session = ClaudeSession(repo_path=REPO, telemetry=SENTINEL)
    assert session._telemetry is SENTINEL


def test_openharness_session_accepts_telemetry_kwarg():
    from warden.providers.openharness.session import OpenHarnessSession

    session = OpenHarnessSession(repo_path=REPO, telemetry=SENTINEL)
    assert session._telemetry is SENTINEL
