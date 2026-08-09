# Telemetry — Real-Time Traces, Metrics, Cost

Always-on instrumentation that streams OTel spans and Langfuse observations during live pipeline execution. Answers: how much did this cost, where is the latency, what's the cache hit rate?

## How It Works

```
ClaudeSession ──► SDK native OTel (env vars) ──► OTel Collector ──► Tempo + Langfuse + Prometheus
OpenHarnessSession ──► OpenLLMetry instrumentor ──► OTel Collector ──► same backends
```

Claude uses native SDK telemetry — the SDK emits tokens, cost (USD), cache breakdown, and trace spans automatically when OTel env vars are set. OpenHarness uses OpenLLMetry to auto-instrument the OpenAI-compatible client.

## Files

| File | Purpose |
|------|---------|
| `setup.py` | OTel + Langfuse configuration — `build_claude_otel_env(cfg)`, `init_openharness_otel(cfg)`, `get_langfuse(cfg)` singleton, output truncation helpers. Each takes an optional `TelemetryConfig` slice (C7) and never reads `get_harness_settings()` at import time. |
| `claude_langfuse_tracer.py` | `ClaudeLangfuseTracer` — wraps Claude SDK stream with Langfuse observation spans (generation, tool use, sub-agent nesting) |
| `openharness_langfuse_tracer.py` | `OpenHarnessLangfuseTracer` — wraps OpenHarness event stream with Langfuse observation spans |

## What Gets Captured

| Attribute | Claude SDK | OpenHarness + OpenLLMetry |
|-----------|-----------|--------------------------|
| `gen_ai.request.model` | Yes | Yes |
| `gen_ai.usage.input_tokens` | Yes | Yes |
| `gen_ai.usage.output_tokens` | Yes | Yes |
| Cache read/creation tokens | Yes | No (Ollama doesn't cache) |
| Cost (USD) | Yes (metric) | No (local/free) |
| Sub-agent nesting | Native (TRACEPARENT) | Manual if needed |
| Tool call spans | Native (`claude_code.tool`) | Not auto-instrumented |

## Backends

| Backend | What it provides |
|---------|-----------------|
| Tempo | Trace waterfall — see the full execution tree |
| Prometheus | Metrics — cost trends, token usage, latency percentiles |
| Grafana | Dashboards — visualize metrics from Prometheus and traces from Tempo |
| Langfuse | LLM analytics — agent execution graphs, session replay, cost per model |

## Reference & Reports

```
telemetry/
├── reference/                          # SDK event dumps — raw provider event streams
│   ├── README.md                       # How dumps were captured, key fields for tracing
│   ├── sdk-messages-dump.json          # Claude SDK: single tool call lifecycle
│   ├── sdk-subagent-dump.json          # Claude SDK: sub-agent lifecycle
│   ├── openharness-events-dump.json    # OpenHarness: single tool call lifecycle
│   └── openharness-agent-dump.json     # OpenHarness: agent lifecycle
└── reports/                            # Smoke test results from specific runs
    ├── 2026-06-22-otel-results.md      # OTel → Tempo + Prometheus validation
    └── 2026-06-22-langfuse-results.md  # Langfuse trace hierarchy validation
```

## Guides

- **Testing instrumentation:** [testing-instrumentation.md](../../docs/guides/observability/testing-instrumentation.md) — verify OTel + Langfuse after code changes
- **Langfuse smoke test:** [langfuse-smoke-test.md](../../docs/guides/observability/langfuse-smoke-test.md) — validate trace hierarchy and metadata
- **OTel smoke test:** [otel-smoke-test.md](../../docs/guides/observability/otel-smoke-test.md) — validate spans reach Tempo and metrics reach Prometheus
