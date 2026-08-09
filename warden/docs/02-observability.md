# The Harness — Observability

> Companion to [`01-conceptual-model.md`](./01-conceptual-model.md). The harness is a **mechanism that emits
> typed signals**; *which backends consume them* is operator config. This doc covers the event stream
> as the primary signal, the two telemetry paths, the provider-shaped visibility gap, and the durable
> event log that is the ideal-state backbone. It is deliberately distinct from [`04-audit.md`](./04-audit.md)
> — see §5.

---

## 1. The principle, applied to observability

Emitting signals is **mechanism** — it belongs in the harness. *Choosing and hosting* the backends
(Tempo, Prometheus, Langfuse), and setting retention, is **operator config** — it belongs to the
deployment. The engine holds no opinion on where a signal goes; it emits, and the deployment routes.
This is the same shape as auth ([§10](./01-conceptual-model.md#s10)): **the environment is the seam** —
telemetry is switched on and pointed at a collector through env vars, not code.

---

## 2. The event stream is the primary signal

Before any external backend exists, the harness's own output contract is already observable: the
typed event stream — `session · token · tool_use · result · error · stopped` ([§3](./01-conceptual-model.md#s3),
[§12](./01-conceptual-model.md#s12)). `result` carries **usage + cost**; `stopped` carries the
governance **reason** (budget/deadline — [`01` §7e](./01-conceptual-model.md#s7e)). A consumer that reads nothing
but the stream already has per-run progress, a tool trace, and the bill. Everything below *enriches*
this stream into queryable stores; it never replaces it.

This matters for the ideal model: observability is not bolted on, it is the **same typed contract**
the drive paths already consume, read a second way.

---

## 3. Two telemetry paths

Every provider has two independent telemetry paths. They answer different questions and share no code:

```
                        Provider (claude / openharness)
                                    │
            ┌───────────────────────┴───────────────────────┐
            ▼                                                ▼
   Path 1: OTel (native / automatic)          Path 2: Langfuse (manual, our code)
            │                                                │
   OTel Collector → Tempo (traces)              Langfuse → sessions · cost · tool
                  → Prometheus (metrics)                    spans · agent nesting
            │                                                │
   "is it healthy, fast, cheap?"                 "what did the agent actually do?"
```

| | **OTel** | **Langfuse** |
|---|---|---|
| Captures | infra spans — model, tokens, latency, cache hits | semantic analytics — which tools, what produced, cost, agent tree |
| Built by | the provider natively (env vars flip it on) | us, by iterating the provider's message stream |
| Feeds | Tempo waterfalls, Prometheus dashboards | the Langfuse session/trace UI |
| Question | operational health | agent behavior + cost attribution |

**The env-var seam.** For the `claude` provider, OTel is native: the harness sets
`CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` into the provider's
`options.env` — the *same* per-run env channel discussed for auth injection
([§10](./01-conceptual-model.md#s10)) — and the SDK emits a two-level span hierarchy
(`interaction` → `llm_request`) automatically. For `openharness` (no native OTel), the harness
instruments the process itself (OpenLLMetry patches the `openai` client). The Langfuse path is always
manual: iterate the stream, map each message/event to an observation.

---

## 4. The provider-shaped visibility gap

Observability depth is **not uniform across providers**, and the reason is the same axis as isolation
([§8](./01-conceptual-model.md#s8)):

- **`claude` (SDK) multiplexes** parent and sub-agent messages onto **one** async stream; sub-agent
  messages carry `parent_tool_use_id`, which the tracer uses as a routing key to nest them. Result:
  **full visibility** — every sub-agent LLM call and tool use appears in the trace, and cost is
  attributed per model.
- **`openharness` isolates** each sub-agent as a **separate subprocess** with its own event loop that
  nobody external iterates. Result: only **spawn/exit** of a sub-agent is visible; its internal LLM
  and tool calls are not.

This is a direct trade-off, not a bug: the isolation that buys OpenHarness parallelism costs it
observability. It is the **same multiplex-vs-isolate split** that shows up in audit hooks
([`04-audit.md`](./04-audit.md) §3) — one process you can see through, one process boundary you can't.

The ideal is **full nested visibility on every provider.** For a process-isolated provider like
OpenHarness the harness reaches it by instrumenting the sub-agent subprocess too — passing `OTEL_*` +
`LANGFUSE_PARENT_OBSERVATION_ID` into the child and running the instrumentor there — so sub-agent work
nests under the parent instead of vanishing at the process boundary.

---

## 5. Observability vs audit

They are different systems, and conflating them muddies both (full treatment in
[`04-audit.md`](./04-audit.md)):

- **Telemetry (this doc)** — real-time, aggregate, operational, **always-on** (infra-level),
  backend-retained (Tempo/Langfuse). Consumers: Grafana dashboards, the Langfuse UI. Answers *"is the
  system healthy, and what did it cost?"*
- **Audit** — per-run, durable, forensic, **opt-in**, file/log-retained. Consumers: aggregation
  reports + human review + permission tuning. Answers *"exactly what did this run do, in order, and
  was it allowed to?"*

They share no code and no data; run them together or independently.

---

## 6. The durable event log — the ideal-state backbone

The productionization design lands on one primitive that is simultaneously the observability
history, the audit trail, the resume log, and the idempotency guard:

```
  run_events(run_id, seq, type, data)
    ├─ seq   monotonic per run (1,2,3…)     → ordering + Last-Event-ID reconnect cursor
    ├─ type  session | checkpoint | result | error | stopped | compaction | …
    └─ PK (run_id, seq)                      → the append IS the idempotency guard
```

Two design points make it earn its place:

1. **The control plane keeps its OWN copy.** The harness has its event log *inside the sandbox*, but
   that dies on teardown. So the app mirrors every event it receives (over the webhook channel) into
   its own `run_events` — history must survive the container.
2. **`seq` is a monotonic version.** A duplicate delivery of `(run_id, seq)` collides on the primary
   key and is a no-op; a browser reconnects with `Last-Event-ID` and the server replays from `seq+1`.

Four independent systems (Polos, Trigger.dev, LangGraph, OpenHands) converged on this shape — the
strongest possible signal it is right.

The durable per-run *log* is what turns observability from "watch it live" into "replay any run, in
order, forever" — the graduation from the ephemeral event stream to a persisted, ordered history that
outlives the run.

---

## 7. The capture matrix

| Signal | `claude` (SDK) | `openharness` |
|---|---|---|
| LLM calls (model, tokens) | ✅ OTel + Langfuse | ✅ OpenLLMetry + Langfuse |
| Tool calls (input/output) | ✅ Langfuse | ✅ Langfuse |
| Sub-agent LLM + tool calls | ✅ (multiplexed) | ✅ (via subprocess instrumentation — §4) |
| Cost | ✅ per-model from `result` | n/a (local Ollama) |
| Prompt/response text | redacted by default | full |

The two providers *reach* the signals differently — Claude multiplexes everything onto one stream,
OpenHarness instruments its subprocess (§4) — but the **captured surface is the same**: LLM calls,
tool calls, sub-agent nesting, and (where the provider bills) cost, with attribute names aligned to
the GenAI semantic conventions. The durable `run_events` log (§6) is the ordered history beneath it.
