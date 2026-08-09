# Observability Baseline — the reference event/telemetry taxonomy

> **What this is.** The **golden baseline** every provider's telemetry is aligned *toward*: the exact
> span tree, attribute names, and Langfuse observations a single agent turn produces. **Claude Code's
> *native* OTEL export is the anchor** — it emits a rich, well-shaped taxonomy for free, so we treat
> that shape as the target and bring OpenHarness/Codex up to parity with it.
>
> **Provenance.** The concrete values below are **captured from real runs** across 3 scenarios
> (no-tool / single-tool / sub-agent). The **shape** is what's normative, not the specific
> model/token numbers. §7 is the recipe to
> regenerate this live.

---

## 1. The base query (how the baseline is produced)

Two canonical runs establish the baseline. Both are documented, reproducible smoke tests
([`testing-instrumentation.md`](./testing-instrumentation.md)):

- **Simple turn** (no tools) — the minimal "fire one query, get a response" baseline:
  > `session.send("What is 3+3? Reply with just the number.")` on `ClaudeSession`.
- **Waterfall turn** (tools + a sub-agent) — the full nested baseline:
  > `warden/scripts/otel-waterfall-test.py` — 5 turns, turn 4 spawns an `Agent` sub-agent (Explore) that runs
  > `Grep` on Haiku while the parent runs on Opus.

Each run fans out into **two independent telemetry paths** (see §2), and the baseline is the union of
what both capture.

## 2. The two telemetry paths (one run, two signals)

```
                         ┌───────────────────────────────────────────────┐
   one agent turn ──────▶│ Provider session (claude / openharness / codex)│
                         └───────────────┬───────────────┬───────────────┘
                                         │               │
              native OTEL export /       │               │   Langfuse Python SDK (direct)
              OpenLLMetry instrumentor    ▼               ▼
                       ┌──────────────────────┐   ┌───────────────────────────┐
                       │   OTEL Collector :4317│   │      Langfuse v2 :3456     │
                       │   ┌────────┐┌────────┐│   │  LLM analytics: prompt /   │
                       │   │traces  ││metrics ││   │  output / cost / session / │
                       │   └───┬────┘└───┬────┘│   │  sub-agent waterfall       │
                       └───────┼─────────┼─────┘   └───────────────────────────┘
                          ┌────▼───┐ ┌───▼────────┐
                          │ Tempo  │ │ Prometheus │   →  Grafana :3030 (ops dashboards)
                          │:3200   │ │ :9090      │      (waterfalls + spanmetrics)
                          └────────┘ └────────────┘
```

- **OTEL path** → **Tempo** (trace waterfalls) + **Prometheus** (spanmetrics) → **Grafana** (ops
  dashboards). *"How is the fleet doing? Show me the waterfall."*
- **Langfuse path** → **Langfuse v2** direct via the Python SDK. *"What was the prompt/output, what did
  it cost, replay the session, see the sub-agent nesting."*

The two are **independent by design** — Tempo never sees Langfuse data and vice-versa. Langfuse uses
the SDK (not OTLP) so the lightweight Postgres-only v2 deployment suffices (v3/OTLP would need
ClickHouse+Redis+MinIO).

## 3. Claude native OTEL — THE ANCHOR taxonomy

This is what Claude Code emits **on its own** once you pass it the OTEL env (§6). Everything else
aligns to this shape:

```
Root span:  claude_code.interaction
  attrs:  session.id · span.type=interaction · interaction.sequence · interaction.duration_ms
          user_prompt=<REDACTED by default> · user_prompt_length
  │
  ├─ Child:  claude_code.llm_request        (Haiku — system-prompt classification pass)
  │     gen_ai.system=anthropic · gen_ai.request.model=claude-haiku-4-5-20251001
  │     input_tokens=442 · output_tokens=11 · cache_read_tokens=0
  │
  └─ Child:  claude_code.llm_request        (Opus — the main response)
        gen_ai.system=anthropic · gen_ai.request.model=claude-opus-4-7[1m]
        input_tokens=6 · output_tokens=6 · cache_read_tokens=21204
        gen_ai.response.finish_reasons=["end_turn"]
```

**Native tool spans** (confirmed 2026-07-21 — Claude emits these *in addition* to the LLM tiers, so
provider tool spans should align to them):
```
claude_code.tool                    attrs: tool_name (e.g. Agent), session.id
claude_code.tool.execution          the actual execution span
claude_code.tool.blocked_on_user    the permission/confirmation wait
```
On a sub-agent turn the trace is ~20 spans: 1 `interaction` + 7 `llm_request` + 4×(`tool` /
`tool.execution` / `tool.blocked_on_user`).

**Normative facts (the shape to match):**
- **Span tiers:** `interaction` (root, carries `session.id`) → `llm_request` (per model call) →
  `tool` / `tool.execution` / `tool.blocked_on_user` (per tool call).
- **GenAI semconv on the wire:** `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.finish_reasons`,
  token attributes. This is the vocabulary every provider's OTEL should speak.
- **Two LLM calls per interaction** is normal for Claude (a Haiku classification pass + the Opus answer).
- **Prompts are redacted by default** in OTEL (`user_prompt=<REDACTED>`); set `OTEL_LOG_USER_PROMPTS=true`
  to log them. Langfuse always has the full prompt.

**Prometheus spanmetrics** the collector derives from these spans (the `phoenix_` prefix comes from the
OTel collector's configured `namespace` — see `infra/observability/otel-collector-config.yaml`; adjust if you change it):
```
phoenix_traces_span_metrics_calls_total{span_name="claude_code.interaction"}
phoenix_traces_span_metrics_calls_total{span_name="claude_code.llm_request", gen_ai_request_model="claude-opus-4-7[1m]"}
phoenix_traces_span_metrics_calls_total{span_name="claude_code.llm_request", gen_ai_request_model="claude-haiku-4-5-20251001"}
```

## 4. Claude Langfuse — the LLM-analytics view

The hand-written `ClaudeLangfuseTracer` produces (simple turn):

```jsonc
// Trace
{ "name": "claude_code.interaction", "sessionId": "...", "input": "What is 3+3?...",
  "output": "6", "latency": 2.983,
  "metadata": { "model": "claude-opus-4-7", "provider": "claude", "total_cost_usd": 0.0505 } }

// Generation observation
{ "name": "claude_code.llm_request", "model": "claude-opus-4-7", "input": "...", "output": "6",
  "promptTokens": 6, "completionTokens": 1, "totalTokens": 7,
  "metadata": { "cache_read_tokens": 14390, "total_cost_usd": 0.0505 } }
```

**Sub-agent nesting** (waterfall turn 4) — the load-bearing capability. Observations nest via
`parent_tool_use_id`:

```
GENERATION  llm_call_1   parent=ROOT          model=claude-opus-4-7
GENERATION  llm_call_2   parent=ROOT          model=claude-opus-4-7
SPAN        tool: Agent  parent=ROOT          ← the sub-agent boundary
GENERATION  llm_call_3   parent=tool: Agent   model=claude-haiku-4-5-20251001   ← nested
SPAN        tool: Grep   parent=tool: Agent                                     ← nested
GENERATION  llm_call_4   parent=ROOT          model=claude-opus-4-7
```

The `tool: Agent` span carries rich metadata: `subagent_type=Explore · status=completed · task_id ·
duration_ms=2979 · total_tokens=11101 · tool_use_count`, plus the sub-agent's prompt (input) and result
(output). Trace-level `model_usage` gives a **per-model cost breakdown** (Opus vs Haiku) for
attribution.

## 5. OpenHarness (Ollama) baseline — and where it diverges

OpenHarness has **no native OTEL** — the `OpenAIInstrumentor` (OpenLLMetry) auto-instruments the
OpenAI-compatible call, producing **one span per LLM call**, not the tiered `interaction → llm_request`
tree:

```
Span:  openai.chat        (service.name=openharness, scope=opentelemetry.instrumentation.openai)
  gen_ai.operation.name=chat · gen_ai.provider.name=openai · gen_ai.request.model=qwen3:8b
  gen_ai.request.max_tokens=4096 · gen_ai.is_streaming=true
  gen_ai.input.messages=[full array]  ← NOT redacted (unlike Claude)
  gen_ai.tool.definitions=[full array]
  gen_ai.usage.input_tokens / gen_ai.usage.output_tokens   (on success; terminal-only)
```

The Langfuse path mirrors Claude's structure (trace + generation + tool spans via the hand-written
`OpenHarnessLangfuseTracer`), but calls are **free** (no dollar cost).

**The three gaps vs the anchor** (what §8 must close):
1. **Different span names / no turn tier** — `openai.chat` only; no `interaction`/`llm_request` tiers,
   no explicit `turn` or `tool_use/tool_result` OTEL spans.
2. **GenAI semconv only in the audit JSONL**, not on the wire `Event` or the Langfuse `generation`
   records.
3. **Sub-agent *internals* vanish** — the OpenHarness parent runs in-process, but sub-agents run in a
   **child subprocess** with no OTEL env / `LANGFUSE_PARENT_OBSERVATION_ID` threaded in and no
   instrumentor running there. Only spawn/exit is visible; the child's LLM + tool calls disappear at
   the process boundary. (Claude gets this for free because the SDK subprocess inherits the OTEL env.)

## 6. Provider attribute matrix (captured)

| Attribute | Claude OTEL | Claude Langfuse | OpenHarness OTEL | OpenHarness Langfuse |
|---|---|---|---|---|
| Model name | `gen_ai.request.model` | `model` | `gen_ai.request.model` | `model` |
| Input tokens | `input_tokens` | `promptTokens` | `gen_ai.usage.input_tokens` (on success) | N/A (free) |
| Output tokens | `output_tokens` | `completionTokens` | `gen_ai.usage.output_tokens` (on success) | N/A (free) |
| Cache tokens | `cache_read_tokens` | metadata | N/A (Ollama no cache) | N/A |
| Cost (USD) | separate metric | `total_cost_usd` | N/A (free) | N/A |
| Session ID | `session.id` | `sessionId` | N/A | `sessionId` |
| Prompt text | redacted by default | full | full | full |
| Response text | not in spans | `output` | not in spans | `output` |

### Claude OTEL env (set via `options.env`, from `build_claude_otel_env()`)
```
CLAUDE_CODE_ENABLE_TELEMETRY=1 · CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 · OTEL_EXPORTER_OTLP_PROTOCOL=grpc · ..._INSECURE=true
OTEL_TRACES_EXPORTER=otlp · OTEL_METRICS_EXPORTER=otlp · OTEL_LOGS_EXPORTER=otlp
OTEL_SERVICE_NAME=claude-agent · CLAUDE_CODE_OTEL_FLUSH_TIMEOUT_MS=10000 · ...
```

## 7. The parity target (what "aligned to the baseline" means)

Every provider should emit **the same taxonomy under the same `gen_ai.*` names**, so a dashboard or a
compaction-parity test can compare them apples-to-apples. The target:

| Baseline signal (from Claude) | claude | codex | openharness |
|---|---|---|---|
| root `interaction` span w/ `session.id` | ✅ native | ➖ (no tracer) | ➖ turns are top-level (no root OTEL span) |
| `llm_request`/`turn` span + `gen_ai.*` on the wire | ✅ (wire Event + Langfuse) | ➖ wire-only | ✅ turn/tool OTEL spans |
| one normalized `{input,output,cached,cost_usd}` usage shape | ✅ `normalize_usage` in runner | ✅ (provider-agnostic) | ✅ (provider-agnostic) |
| `tool_use`/`tool_result` spans | ✅ | ➖ wire-only | ✅ (OTEL tool spans) |
| sub-agent **boundary** (spawn/exit, type, status, aggregate tokens) | ✅ native | n/a | ✅ (at `tool: Agent`, `internals_captured:false`) |
| sub-agent **internals** nested across the subprocess boundary | ✅ native | n/a | ⛔ **deferred** (needs OpenHarness library change) |
| per-run cost + per-model breakdown | ✅ | ➖ | free (no $) |
| cross-provider taxonomy-parity test | ✅ hermetic lock + live gate | ➖ | ✅ |

**Status:** the taxonomy is delivered for the wire (all providers), Claude, and OpenHarness on both paths —
Langfuse gen_ai.* + Claude sub-agent nesting, Tempo spans incl. the OpenHarness turn span, and an
`enable_telemetry` off-switch → zero telemetry. The ➖ **codex** cells reflect that codex has **no dedicated
in-process OTEL/Langfuse tracer** — the provider-agnostic wire-Event semconv covers it at the `Event` level,
but a codex tracer + native OTEL parity is **deferred** (revisit if codex telemetry is needed).
**The ⛔ row is out of scope** — OpenHarness sub-agent *internals* run
in a child subprocess owned by the OpenHarness library, so instrumenting inside would require changing that
library; we capture the **boundary** only and mark `internals_captured: false`. This does not block
compaction-parity (boundary aggregates suffice for cost/parity); it only limits step-level debugging
*inside* an OpenHarness sub-agent.

## 8. Regenerating the baseline live

The 2026-06-22 capture can be reproduced whenever the stack is up:

1. Bring up the observability stack (Langfuse :3456, OTEL collector :4317, Tempo :3200, Prometheus :9090,
   Grafana :3030) — see [`testing-instrumentation.md`](./testing-instrumentation.md) prerequisites.
2. **Simple baseline:** run Test 3 (Claude simple) → verify the Langfuse trace = `claude_code.interaction`
   with `total_cost_usd > 0`.
3. **OTEL baseline:** run [`otel-smoke-test.md`](./otel-smoke-test.md) → verify Tempo shows
   `claude_code.interaction` → `claude_code.llm_request` with `gen_ai.request.model`, and Prometheus has
   the spanmetrics.
4. **Waterfall + sub-agent baseline:** run `warden/scripts/otel-waterfall-test.py` → verify turn-4 sub-agent
   nesting in Langfuse.
5. Write results to `observability/telemetry/reports/YYYY-MM-DD-*.md` and, if the shape changed, update
   §3–§6 here.

**Cost discipline:** OAuth for Claude, free Ollama `qwen3:8b` for OpenHarness — never the API-key lane.
