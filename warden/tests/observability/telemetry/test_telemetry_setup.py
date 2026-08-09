"""Hermetic unit tests for harness telemetry setup/config helpers.

Covers ``warden/orchestrator/observability/telemetry/setup.py``:
    - build_claude_otel_env  — OTel env dict for the Claude SDK
    - get_tool_output_limit  — env-configured char limit
    - truncate_output        — output truncation helper
    - get_langfuse           — lazy singleton accessor (mocked client)
    - init_openharness_otel  — guarded/idempotent OTel init (mocked deps)
    - shutdown_langfuse      — flush/shutdown + idempotency

All external deps (Langfuse client, OTel instrumentor) are mocked — no
network. Module-level globals are reset before each test for order
independence (LAW: hermetic).
"""

from __future__ import annotations

import sys

import pytest

from warden.config import get_harness_settings
from warden.config.models import TelemetryConfig
from warden.observability.telemetry import setup


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """Reset the telemetry module globals before and after each test.

    ``setup`` caches the Langfuse client and OTel-init flag in module-level
    globals. Without resetting them, singleton/idempotency tests would leak
    state into each other and become order-dependent.

    Also clears the ``get_harness_settings`` lru_cache: the telemetry helpers
    now read config through it, so a stale cached ``HarnessSettings`` would
    hide env vars a test sets via monkeypatch and make the tests
    order-dependent.
    """
    def _reset():
        setup._langfuse_client = None
        setup._langfuse_initialized = False
        setup._otel_initialized = False
        get_harness_settings.cache_clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# build_claude_otel_env
# ---------------------------------------------------------------------------


def test_build_claude_otel_env_includes_static_keys():
    env = setup.build_claude_otel_env(TelemetryConfig(enable_telemetry=True))
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
    assert env["OTEL_EXPORTER_OTLP_INSECURE"] == "true"
    assert env["OTEL_SERVICE_NAME"] == "claude-agent"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"


def test_build_claude_otel_env_wires_endpoint_default(monkeypatch):
    # With no injected cfg, the endpoint falls back to the typed config surface
    # (HarnessSettings default when OTEL_COLLECTOR_ENDPOINT is unset).
    monkeypatch.delenv("OTEL_COLLECTOR_ENDPOINT", raising=False)
    get_harness_settings.cache_clear()
    # enable_telemetry=True opts in; endpoint still defaults to localhost:4317.
    env = setup.build_claude_otel_env(TelemetryConfig(enable_telemetry=True))
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"


def test_build_claude_otel_env_uses_injected_config():
    # C7 (M8): an injected TelemetryConfig slice wins — resolved at call time,
    # no get_harness_settings() read.
    env = setup.build_claude_otel_env(
        TelemetryConfig(
            enable_telemetry=True, otel_collector_endpoint="http://collector:9999"
        )
    )
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:9999"


def test_build_claude_otel_env_returns_new_dict():
    # Callers must not be able to mutate the shared CLAUDE_OTEL_ENV constant.
    # enable_telemetry=True so the returned dict is non-empty and the
    # mutation-isolation check is meaningful.
    env = setup.build_claude_otel_env(TelemetryConfig(enable_telemetry=True))
    env["OTEL_SERVICE_NAME"] = "mutated"
    assert setup.CLAUDE_OTEL_ENV["OTEL_SERVICE_NAME"] == "claude-agent"


def test_no_import_time_settings_read():
    # C7 (M8): the module must not expose an import-time-resolved endpoint
    # constant — the read was moved into the functions (lazy/injected).
    assert not hasattr(setup, "OTEL_COLLECTOR_ENDPOINT")


def test_telemetry_helpers_do_not_read_get_harness_settings(monkeypatch):
    # C7: setup.py is a config leaf — its helpers resolve through the typed
    # config surface (get_harness_config), never get_harness_settings() directly.
    import warden.config as cfg_pkg

    def _boom(*a, **k):  # pragma: no cover - only fires on a regression
        raise AssertionError("telemetry leaf must not read get_harness_settings()")

    monkeypatch.setattr(cfg_pkg, "get_harness_settings", _boom)
    # setup imported the name into its own namespace? It must not have.
    assert not hasattr(setup, "get_harness_settings")

    tc = TelemetryConfig(
        enable_telemetry=True,
        otel_collector_endpoint="http://c:1",
        langfuse_tool_output_limit=42,
    )
    # With an injected slice, no config read happens at all.
    assert setup.build_claude_otel_env(tc)["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://c:1"
    assert setup.get_tool_output_limit(tc) == 42


# ---------------------------------------------------------------------------
# M3 4b — enable_telemetry master switch (off-switch gates both OTEL paths)
# ---------------------------------------------------------------------------


def test_build_claude_otel_env_empty_when_disabled():
    # Master switch off -> NO OTEL env at all.
    assert setup.build_claude_otel_env(TelemetryConfig(enable_telemetry=False)) == {}


def test_build_claude_otel_env_default_surface_is_off(monkeypatch):
    # No injected cfg + default config surface -> telemetry off by default, so
    # the returned env is empty (proves default-off).
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_TELEMETRY", raising=False)
    get_harness_settings.cache_clear()
    assert setup.build_claude_otel_env() == {}


def test_build_claude_otel_env_on_switch_wires_keys_and_endpoint():
    # Master switch on -> static enable key AND endpoint present together.
    env = setup.build_claude_otel_env(
        TelemetryConfig(enable_telemetry=True, otel_collector_endpoint="http://x:4317")
    )
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://x:4317"


def test_init_openharness_otel_noop_when_disabled(monkeypatch):
    # Master switch off -> pure no-op, never touches OTel and leaves the flag
    # False so a later enabled call can still initialize. The opentelemetry=None
    # guard proves the import is never attempted (no exception raised).
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    setup._otel_initialized = False

    setup.init_openharness_otel(TelemetryConfig(enable_telemetry=False))

    assert setup._otel_initialized is False


# ---------------------------------------------------------------------------
# get_tool_output_limit
# ---------------------------------------------------------------------------


def test_get_tool_output_limit_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_TOOL_OUTPUT_LIMIT", raising=False)
    assert setup.get_tool_output_limit() == 500


def test_get_tool_output_limit_env_override(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "1234")
    assert setup.get_tool_output_limit() == 1234


def test_get_tool_output_limit_negative_and_zero(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "-1")
    assert setup.get_tool_output_limit() == -1
    # HarnessSettings is lru_cached; clear it so the new env value is re-read.
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "0")
    get_harness_settings.cache_clear()
    assert setup.get_tool_output_limit() == 0


def test_get_tool_output_limit_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "not-a-number")
    assert setup.get_tool_output_limit() == 500


def test_get_tool_output_limit_reads_from_settings(monkeypatch):
    # The limit is sourced from HarnessSettings, not a direct os.environ read.
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "77")
    get_harness_settings.cache_clear()
    assert get_harness_settings().langfuse_tool_output_limit == 77
    assert setup.get_tool_output_limit() == 77


# ---------------------------------------------------------------------------
# truncate_output
# ---------------------------------------------------------------------------


def test_truncate_output_none():
    assert setup.truncate_output(None) == "(no output)"


def test_truncate_output_under_limit_passthrough():
    assert setup.truncate_output("hello", limit=100) == "hello"


def test_truncate_output_at_boundary_passthrough():
    text = "x" * 10
    assert setup.truncate_output(text, limit=10) == text


def test_truncate_output_over_limit_truncates():
    text = "x" * 20
    result = setup.truncate_output(text, limit=10)
    assert result == "x" * 10 + "... (20 chars total)"


def test_truncate_output_zero_suppresses():
    assert setup.truncate_output("anything", limit=0) == "(captured, output suppressed)"


def test_truncate_output_negative_returns_full():
    text = "x" * 5000
    assert setup.truncate_output(text, limit=-1) == text


def test_truncate_output_uses_env_default_when_limit_none(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TOOL_OUTPUT_LIMIT", "3")
    result = setup.truncate_output("abcdefgh")
    assert result == "abc... (8 chars total)"


def test_truncate_output_default_limit_500(monkeypatch):
    monkeypatch.delenv("LANGFUSE_TOOL_OUTPUT_LIMIT", raising=False)
    text = "y" * 600
    result = setup.truncate_output(text)
    assert result == "y" * 500 + "... (600 chars total)"


# ---------------------------------------------------------------------------
# get_langfuse — fake client injected via a stub ``langfuse`` module
# ---------------------------------------------------------------------------


class _FakeLangfuse:
    """Stand-in for ``langfuse.Langfuse`` — records ctor args, no network."""

    instances: list["_FakeLangfuse"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.auth_ok = True
        self.flushed = False
        self.shut_down = False
        _FakeLangfuse.instances.append(self)

    def auth_check(self):
        return self.auth_ok

    def flush(self):
        self.flushed = True

    def shutdown(self):
        self.shut_down = True


@pytest.fixture
def fake_langfuse_module(monkeypatch):
    """Install a stub ``langfuse`` module so ``from langfuse import Langfuse``
    inside get_langfuse resolves to _FakeLangfuse without any real import."""
    import types

    _FakeLangfuse.instances = []
    module = types.ModuleType("langfuse")
    module.Langfuse = _FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    return module


def test_get_langfuse_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup.get_langfuse() is None


def test_get_langfuse_returns_none_when_only_public_key(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup.get_langfuse() is None


def test_get_langfuse_constructs_client_when_configured(monkeypatch, fake_langfuse_module):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-123")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-456")
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf.example:3456")

    client = setup.get_langfuse()

    assert isinstance(client, _FakeLangfuse)
    assert client.kwargs["public_key"] == "pk-123"
    assert client.kwargs["secret_key"] == "sk-456"
    assert client.kwargs["host"] == "http://lf.example:3456"
    # Values came through HarnessSettings, not a direct os.environ read.
    settings = get_harness_settings()
    assert settings.langfuse_public_key == "pk-123"
    assert settings.langfuse_secret_key == "sk-456"
    assert settings.langfuse_host == "http://lf.example:3456"


def test_get_langfuse_default_host(monkeypatch, fake_langfuse_module):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    client = setup.get_langfuse()
    assert client.kwargs["host"] == "http://localhost:3456"


def test_get_langfuse_caches_client(monkeypatch, fake_langfuse_module):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    first = setup.get_langfuse()
    second = setup.get_langfuse()

    assert first is second
    # Constructed exactly once despite two calls.
    assert len(_FakeLangfuse.instances) == 1


def test_get_langfuse_caches_none_result(monkeypatch, fake_langfuse_module):
    # First call unconfigured -> None and marks initialized; a later call must
    # not re-attempt construction even if keys appear afterward.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert setup.get_langfuse() is None

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert setup.get_langfuse() is None
    assert _FakeLangfuse.instances == []


def test_get_langfuse_none_on_auth_failure(monkeypatch, fake_langfuse_module):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    # Make the fake client fail its auth check.
    original_init = _FakeLangfuse.__init__

    def _failing_init(self, **kwargs):
        original_init(self, **kwargs)
        self.auth_ok = False

    monkeypatch.setattr(_FakeLangfuse, "__init__", _failing_init)

    assert setup.get_langfuse() is None


def test_get_langfuse_none_on_constructor_exception(monkeypatch, fake_langfuse_module):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")

    def _boom(self, **kwargs):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(_FakeLangfuse, "__init__", _boom)

    assert setup.get_langfuse() is None


# ---------------------------------------------------------------------------
# shutdown_langfuse
# ---------------------------------------------------------------------------


def test_shutdown_langfuse_noop_when_no_client():
    # No client set — must not raise.
    setup._langfuse_client = None
    setup.shutdown_langfuse()  # should be a silent no-op


def test_shutdown_langfuse_flushes_and_resets():
    fake = _FakeLangfuse()
    setup._langfuse_client = fake
    setup._langfuse_initialized = True

    setup.shutdown_langfuse()

    assert fake.flushed is True
    assert fake.shut_down is True
    assert setup._langfuse_client is None
    assert setup._langfuse_initialized is False


def test_shutdown_langfuse_idempotent():
    fake = _FakeLangfuse()
    setup._langfuse_client = fake
    setup._langfuse_initialized = True

    setup.shutdown_langfuse()
    # Second call has nothing to shut down and must not raise.
    setup.shutdown_langfuse()

    assert setup._langfuse_client is None


def test_shutdown_langfuse_swallows_flush_error():
    class _BadClient:
        def flush(self):
            raise RuntimeError("flush boom")

        def shutdown(self):
            raise RuntimeError("shutdown boom")

    setup._langfuse_client = _BadClient()
    setup._langfuse_initialized = True

    # Errors are logged/swallowed; global still reset in finally.
    setup.shutdown_langfuse()
    assert setup._langfuse_client is None
    assert setup._langfuse_initialized is False


# ---------------------------------------------------------------------------
# init_openharness_otel — guarded/idempotent with mocked OTel deps
# ---------------------------------------------------------------------------


def test_init_openharness_otel_noop_when_already_initialized(monkeypatch):
    setup._otel_initialized = True

    # If it were to run, it would import opentelemetry; guard against that by
    # making the import fail — the early return means it never gets there.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    setup.init_openharness_otel()  # returns immediately, no exception
    assert setup._otel_initialized is True


def test_init_openharness_otel_swallows_import_failure(monkeypatch):
    # Force the OTel imports to fail; init must catch and leave flag False.
    # enable_telemetry=True opts past the master switch so the import is actually
    # ATTEMPTED (and then fails) — the point of this test.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    setup._otel_initialized = False

    setup.init_openharness_otel(TelemetryConfig(enable_telemetry=True))

    # Init failed silently -> not marked initialized, so a later real attempt
    # can still succeed.
    assert setup._otel_initialized is False


def test_init_openharness_otel_success_path(monkeypatch):
    """Drive the happy path with fully stubbed OTel modules (no real SDK)."""
    import types

    calls: dict[str, object] = {}

    # opentelemetry.trace
    trace_mod = types.ModuleType("opentelemetry.trace")

    def _set_tracer_provider(provider):
        calls["provider"] = provider

    trace_mod.set_tracer_provider = _set_tracer_provider

    otel_mod = types.ModuleType("opentelemetry")
    otel_mod.trace = trace_mod

    # OTLP exporter
    exporter_mod = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    )

    class _OTLPSpanExporter:
        def __init__(self, endpoint=None, insecure=None):
            calls["endpoint"] = endpoint
            calls["insecure"] = insecure

    exporter_mod.OTLPSpanExporter = _OTLPSpanExporter

    # OpenAI instrumentor
    instr_mod = types.ModuleType("opentelemetry.instrumentation.openai")

    class _OpenAIInstrumentor:
        def instrument(self):
            calls["instrumented"] = True

    instr_mod.OpenAIInstrumentor = _OpenAIInstrumentor

    # Resources
    resources_mod = types.ModuleType("opentelemetry.sdk.resources")

    class _Resource:
        @staticmethod
        def create(attrs):
            calls["resource_attrs"] = attrs
            return attrs

    resources_mod.Resource = _Resource

    # TracerProvider
    sdk_trace_mod = types.ModuleType("opentelemetry.sdk.trace")

    class _TracerProvider:
        def __init__(self, resource=None):
            self.resource = resource
            self.processors: list = []

        def add_span_processor(self, proc):
            self.processors.append(proc)

    sdk_trace_mod.TracerProvider = _TracerProvider

    # BatchSpanProcessor
    export_mod = types.ModuleType("opentelemetry.sdk.trace.export")

    class _BatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    export_mod.BatchSpanProcessor = _BatchSpanProcessor

    for name, mod in {
        "opentelemetry": otel_mod,
        "opentelemetry.trace": trace_mod,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": exporter_mod,
        "opentelemetry.instrumentation.openai": instr_mod,
        "opentelemetry.sdk.resources": resources_mod,
        "opentelemetry.sdk.trace": sdk_trace_mod,
        "opentelemetry.sdk.trace.export": export_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    setup._otel_initialized = False

    setup.init_openharness_otel(
        TelemetryConfig(
            enable_telemetry=True, otel_collector_endpoint="http://collector:4317"
        )
    )

    assert setup._otel_initialized is True
    assert calls["endpoint"] == "http://collector:4317"
    assert calls["insecure"] is True
    assert calls["instrumented"] is True
    assert calls["resource_attrs"] == {"service.name": setup.OPENHARNESS_SERVICE_NAME}


def test_init_openharness_otel_provider_set_when_instrumentor_absent(monkeypatch):
    """M3 3b — the manual turn/tool spans depend only on the TracerProvider, NOT
    on OpenLLMetry's OpenAIInstrumentor. When that optional package is absent the
    provider must STILL be set (``_otel_initialized`` True) so the manual spans
    reach the collector; only the LLM-call auto-spans are skipped."""
    import types

    calls: dict[str, object] = {}

    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.set_tracer_provider = lambda provider: calls.__setitem__("provider", provider)
    otel_mod = types.ModuleType("opentelemetry")
    otel_mod.trace = trace_mod

    exporter_mod = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    )

    class _OTLPSpanExporter:
        def __init__(self, endpoint=None, insecure=None):
            calls["endpoint"] = endpoint

    exporter_mod.OTLPSpanExporter = _OTLPSpanExporter

    resources_mod = types.ModuleType("opentelemetry.sdk.resources")

    class _Resource:
        @staticmethod
        def create(attrs):
            return attrs

    resources_mod.Resource = _Resource

    sdk_trace_mod = types.ModuleType("opentelemetry.sdk.trace")

    class _TracerProvider:
        def __init__(self, resource=None):
            self.processors: list = []

        def add_span_processor(self, proc):
            self.processors.append(proc)

    sdk_trace_mod.TracerProvider = _TracerProvider

    export_mod = types.ModuleType("opentelemetry.sdk.trace.export")

    class _BatchSpanProcessor:
        def __init__(self, exporter):
            self.exporter = exporter

    export_mod.BatchSpanProcessor = _BatchSpanProcessor

    for name, mod in {
        "opentelemetry": otel_mod,
        "opentelemetry.trace": trace_mod,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": exporter_mod,
        "opentelemetry.sdk.resources": resources_mod,
        "opentelemetry.sdk.trace": sdk_trace_mod,
        "opentelemetry.sdk.trace.export": export_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    # The optional instrumentor is ABSENT: importing from it raises ImportError.
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.openai", None)

    from warden.config.models import TelemetryConfig

    setup._otel_initialized = False
    setup.init_openharness_otel(
        TelemetryConfig(enable_telemetry=True, otel_collector_endpoint="http://c:4317")
    )

    # Provider was set up (manual spans will work) despite the missing instrumentor.
    assert setup._otel_initialized is True
    assert "provider" in calls
    assert calls["endpoint"] == "http://c:4317"


def test_init_openharness_otel_idempotent_after_success(monkeypatch):
    # After a successful init, a second call must be a pure no-op even if the
    # OTel imports would now fail.
    setup._otel_initialized = True
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    setup.init_openharness_otel()

    assert setup._otel_initialized is True
