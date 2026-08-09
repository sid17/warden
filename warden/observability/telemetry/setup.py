"""Shared telemetry setup — OTel constants, Langfuse SDK, helpers.

Two telemetry paths, configured here:
    Path 1: OTel  — Claude uses native SDK env vars, OpenHarness uses OpenLLMetry
    Path 2: Langfuse — both providers use the shared client from get_langfuse()

Usage:
    from warden.observability.telemetry.setup import get_langfuse
    from warden.observability.telemetry.setup import build_claude_otel_env, init_openharness_otel

C7 (M8): this module is a config leaf. Every helper takes an optional
``TelemetryConfig`` slice and resolves it lazily via the typed config surface
(:func:`get_harness_config`) — it never reads ``get_harness_settings()``
directly and never at import time. M3 (doc 04) threads the injected slice
through the tracers and gives ``enable_telemetry`` teeth; here we only lay the
wire.
"""

import logging

from pydantic import ValidationError

from warden.config import get_harness_config
from warden.config.models import TelemetryConfig

logger = logging.getLogger(__name__)


def _telemetry(cfg: TelemetryConfig | None) -> TelemetryConfig:
    """Resolve the telemetry config slice: the injected ``cfg`` when given, else
    the typed config surface. Never ``get_harness_settings()`` directly, never at
    import time (C7)."""
    if cfg is not None:
        return cfg
    return get_harness_config().observability.telemetry


# ---------------------------------------------------------------------------
# OTel constants
# ---------------------------------------------------------------------------

# Env vars passed to Claude SDK subprocess to enable its native OTel export
CLAUDE_OTEL_ENV: dict[str, str] = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_INSECURE": "true",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
    "OTEL_BSP_SCHEDULE_DELAY": "1000",
    "OTEL_BSP_EXPORT_TIMEOUT": "5000",
    "OTEL_METRIC_EXPORT_INTERVAL": "1000",
    "OTEL_TRACES_EXPORT_INTERVAL": "1000",
    "CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS": "10000",
    "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS": "10000",
    "OTEL_SERVICE_NAME": "claude-agent",
}

OPENHARNESS_SERVICE_NAME = "openharness"


def build_claude_otel_env(cfg: TelemetryConfig | None = None) -> dict[str, str]:
    """Return env vars for Claude SDK OTel, endpoint resolved from the injected
    ``cfg`` slice (or the config surface when ``None``).

    M3 4b — ``enable_telemetry`` is the master OTEL switch: when it is False
    (the default) this returns an EMPTY dict, so ClaudeSession injects NO OTEL
    env and a telemetry-free run is expressible in config alone.
    """
    tel = _telemetry(cfg)
    if not tel.enable_telemetry:
        return {}
    return {
        **CLAUDE_OTEL_ENV,
        "OTEL_EXPORTER_OTLP_ENDPOINT": tel.otel_collector_endpoint,
    }


# ---------------------------------------------------------------------------
# OpenHarness OTel — OpenLLMetry instrumentor (idempotent, in-process)
# ---------------------------------------------------------------------------

_otel_initialized = False


def init_openharness_otel(cfg: TelemetryConfig | None = None) -> None:
    """Configure the OTel TracerProvider for OpenHarness (+ optional OpenAI auto-
    instrumentation).

    Sets a ``TracerProvider`` with an OTLP exporter so the harness's manual
    **turn/tool** spans (M3 3b — via ``OpenHarnessOtelTracer``/``get_tracer``)
    reach the collector. The endpoint comes from the injected ``cfg`` slice (or
    the config surface when ``None``). Safe to call multiple times — only
    initializes once.

    OpenLLMetry's ``OpenAIInstrumentor`` (LLM-call auto-spans) is an OPTIONAL
    enhancement: its absence must NOT disable the manual turn/tool spans, so it
    is imported/instrumented in a nested best-effort block AFTER the provider is
    live — the provider needs only ``opentelemetry-sdk`` + the OTLP exporter.
    """
    global _otel_initialized
    if _otel_initialized:
        return
    tel = _telemetry(cfg)
    if not tel.enable_telemetry:
        # M3 4b — master OTEL switch off (default): no-op, leave _otel_initialized
        # False so a later enabled call can still initialize.
        return
    endpoint = tel.otel_collector_endpoint
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": OPENHARNESS_SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _otel_initialized = True
        logger.info("OpenHarness OTel enabled → %s", endpoint)
    except Exception:
        logger.warning(
            "OpenHarness OTel init failed — continuing without", exc_info=True,
        )
        return
    # Optional LLM-call auto-instrumentation. A missing OpenLLMetry package must
    # not disable the manual turn/tool spans set up above (3b).
    try:
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor

        OpenAIInstrumentor().instrument()
    except Exception:
        logger.info(
            "OpenAIInstrumentor unavailable — LLM-call auto-spans off; manual "
            "turn/tool spans still active",
        )


# ---------------------------------------------------------------------------
# Langfuse shared client
# ---------------------------------------------------------------------------

_langfuse_client = None
_langfuse_initialized = False


def get_langfuse(cfg: TelemetryConfig | None = None):
    """Return a shared Langfuse client (lazy-init, singleton).

    Reads the telemetry config slice (injected ``cfg`` or the config surface):
        langfuse_public_key  — project public key (LANGFUSE_PUBLIC_KEY)
        langfuse_secret_key  — project secret key (LANGFUSE_SECRET_KEY)
        langfuse_host        — Langfuse URL (LANGFUSE_HOST, default: http://localhost:3456)

    Returns None if keys are not set (Langfuse is optional).
    """
    global _langfuse_client, _langfuse_initialized

    if _langfuse_initialized:
        return _langfuse_client

    _langfuse_initialized = True

    tc = _telemetry(cfg)
    public_key = tc.langfuse_public_key
    secret_key = tc.langfuse_secret_key

    if not public_key or not secret_key:
        logger.info("Langfuse not configured (LANGFUSE_PUBLIC_KEY/SECRET_KEY not set)")
        return None

    try:
        from langfuse import Langfuse

        host = tc.langfuse_host
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            flush_interval=1.0,
        )
        # Verify connection
        if _langfuse_client.auth_check():
            logger.info("Langfuse connected → %s", host)
        else:
            logger.warning("Langfuse auth check failed — traces will not be sent")
            _langfuse_client = None
    except Exception:
        logger.warning("Langfuse init failed — continuing without", exc_info=True)
        _langfuse_client = None

    return _langfuse_client


def get_tool_output_limit(cfg: TelemetryConfig | None = None) -> int:
    """Return the max chars for tool output in Langfuse observations.

    Controlled by LANGFUSE_TOOL_OUTPUT_LIMIT (via the telemetry config slice):
        0   = don't capture tool output (shows "(completed)")
        500 = truncate to 500 chars (default — good for dev)
        -1  = full content (verbose, for deep debugging)

    A non-integer LANGFUSE_TOOL_OUTPUT_LIMIT makes the config fail to validate;
    preserve the prior behavior of falling back to 500 in that case.
    """
    try:
        return _telemetry(cfg).langfuse_tool_output_limit
    except ValidationError:
        return 500


def truncate_output(
    text: str | None,
    limit: int | None = None,
    cfg: TelemetryConfig | None = None,
) -> str:
    """Truncate text to the configured tool output limit.

    Args:
        text: The text to truncate. None returns "(no output)".
        limit: Override the config limit. Pass 0 to suppress, -1 for full.
        cfg: Optional telemetry config slice used when ``limit`` is None.
    """
    if text is None:
        return "(no output)"
    if limit is None:
        limit = get_tool_output_limit(cfg)
    if limit == 0:
        return "(captured, output suppressed)"
    if limit < 0:
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + f"... ({len(text)} chars total)"


def shutdown_langfuse():
    """Flush and shut down the Langfuse client."""
    global _langfuse_client, _langfuse_initialized
    if _langfuse_client:
        try:
            _langfuse_client.flush()
            _langfuse_client.shutdown()
        except Exception:
            logger.debug("Langfuse shutdown error", exc_info=True)
        finally:
            _langfuse_client = None
            _langfuse_initialized = False
