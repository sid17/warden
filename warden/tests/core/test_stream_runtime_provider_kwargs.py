"""C7 (M8): assemble_turn_provider_kwargs threads the ProviderConfig slice to
the openharness session ctor only — never to providers whose ctor would reject
the unknown kwarg.
"""

from __future__ import annotations

from warden.config.models import AuditConfig, ProviderConfig, TelemetryConfig
from warden.orchestrator.stream_runtime import (
    assemble_turn_provider_kwargs,
)

PC = ProviderConfig(openharness_base_url="http://injected:1")
TC = TelemetryConfig(otel_collector_endpoint="http://sentinel:4317")


def test_provider_config_injected_for_openharness():
    out = assemble_turn_provider_kwargs(
        {}, "openharness", auth_env=None, codex_allow_ungated=False,
        provider_config=PC,
    )
    assert out["provider_config"] is PC


def test_provider_config_not_injected_for_other_providers():
    for provider in ("claude", "codex"):
        out = assemble_turn_provider_kwargs(
            {}, provider, auth_env=None, codex_allow_ungated=False,
            provider_config=PC,
        )
        assert "provider_config" not in out


def test_provider_config_absent_when_none():
    out = assemble_turn_provider_kwargs(
        {}, "openharness", auth_env=None, codex_allow_ungated=False,
        provider_config=None,
    )
    assert "provider_config" not in out


# --- M3 rung 4a: telemetry slice threading -------------------------------


def test_telemetry_injected_for_claude():
    out = assemble_turn_provider_kwargs(
        {}, "claude", auth_env=None, codex_allow_ungated=False,
        telemetry=TC,
    )
    assert out["telemetry"] is TC


def test_telemetry_injected_for_openharness():
    out = assemble_turn_provider_kwargs(
        {}, "openharness", auth_env=None, codex_allow_ungated=False,
        telemetry=TC,
    )
    assert out["telemetry"] is TC


def test_telemetry_not_injected_for_codex():
    out = assemble_turn_provider_kwargs(
        {}, "codex", auth_env=None, codex_allow_ungated=False,
        telemetry=TC,
    )
    assert "telemetry" not in out


def test_telemetry_absent_when_none():
    for provider in ("claude", "openharness", "codex"):
        out = assemble_turn_provider_kwargs(
            {}, provider, auth_env=None, codex_allow_ungated=False,
            telemetry=None,
        )
        assert "telemetry" not in out


# --- M5 rung 3b: audit slice threading (codex now included) ---------------

AC = AuditConfig(enabled=True, run_id="run-x")


def test_audit_injected_for_codex():
    out = assemble_turn_provider_kwargs(
        {}, "codex", auth_env=None, codex_allow_ungated=False,
        audit=AC,
    )
    assert out["audit"] is AC


def test_audit_injected_for_claude_and_openharness():
    for provider in ("claude", "openharness"):
        out = assemble_turn_provider_kwargs(
            {}, provider, auth_env=None, codex_allow_ungated=False,
            audit=AC,
        )
        assert out["audit"] is AC


def test_audit_absent_when_none():
    for provider in ("claude", "openharness", "codex"):
        out = assemble_turn_provider_kwargs(
            {}, provider, auth_env=None, codex_allow_ungated=False,
            audit=None,
        )
        assert "audit" not in out
