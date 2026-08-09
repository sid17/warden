"""P1 tests for the nested HarnessConfig — the flat env layer maps into the
nested declarative surface, preserving the existing (flat) env var names.

Importable without the heavy SDK deps (config imports only pydantic-settings +
pydantic).
"""

from warden.config import (
    HarnessConfig,
    HarnessSettings,
    get_harness_config,
    get_harness_settings,
)


def test_defaults_nested():
    cfg = HarnessConfig.from_settings(HarnessSettings(_env_file=None))
    assert cfg.provider.provider == "claude"
    assert cfg.provider.openharness_model == "qwen3:1.7b"
    assert cfg.concurrency.max_concurrent == 8
    assert cfg.observability.telemetry.langfuse_host == "http://localhost:3456"
    assert cfg.observability.audit.enabled is False
    assert cfg.persistence.backend == "local"
    assert cfg.safety.classifiers.ollama_model == "gemma3:4b"


def test_flat_env_names_map_into_nested_sections(monkeypatch):
    # The existing FLAT env var names must still work — routed into nested sub-configs.
    monkeypatch.setenv("LANGFUSE_HOST", "http://flat:9999")
    monkeypatch.setenv("AUDIT_ENABLED", "true")
    monkeypatch.setenv("WARDEN_CONCURRENCY", "16")
    monkeypatch.setenv("OPENHARNESS_MODEL", "qwen3:8b")
    monkeypatch.setenv("AWS_BUCKET_NAME", "my-bucket")

    cfg = HarnessConfig.from_settings(HarnessSettings(_env_file=None))
    assert cfg.observability.telemetry.langfuse_host == "http://flat:9999"
    assert cfg.observability.audit.enabled is True
    assert cfg.concurrency.max_concurrent == 16
    assert cfg.provider.openharness_model == "qwen3:8b"
    assert cfg.persistence.s3.bucket == "my-bucket"


def test_programmatic_sections_default_empty():
    # Middleware / permissions / custom_tools are set by the app, not env.
    cfg = HarnessConfig.from_settings(HarnessSettings(_env_file=None))
    assert cfg.middleware.input == []
    assert cfg.permissions.handler == "auto_allow"
    assert cfg.custom_tools.tools == []


def test_read_point_and_backcompat():
    # get_harness_config() is the single read point; the flat settings accessor
    # remains for existing importers.
    assert isinstance(get_harness_config(), HarnessConfig)
    assert get_harness_settings().harness_storage_backend == "local"


def test_promoted_safety_knob_env(monkeypatch):
    monkeypatch.setenv("SAFETY_FUZZY_THRESHOLD", "0.85")
    cfg = HarnessConfig.from_settings(HarnessSettings(_env_file=None))
    assert cfg.safety.classifiers.fuzzy_threshold == 0.85
