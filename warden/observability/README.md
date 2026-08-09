# observability/ — L3: telemetry + audit

> **Layer / role:** L3 cross-cutting — the signals every run emits; a peer of `persistence/` and `safety/`.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §5 L3, §13; detailed in [`02-observability.md`](../docs/02-observability.md) and [`04-audit.md`](../docs/04-audit.md).
> **Inbound:** wraps every run driven by the orchestrator (L2).
> **Outbound:** `schemas/audit.py` (the `AuditEvent` contract); external backends (OTel Collector → Tempo/Prometheus, Langfuse).

Two complementary systems for understanding what a run is doing and how well it's doing it.

```
observability/
├── telemetry/     # Real-time: OTel + Langfuse (always on)     — 02-observability.md
└── audit/         # Offline: JSONL hook logging (on-demand)    — 04-audit.md
```

## Telemetry vs Audit — Why Both?

| | [Telemetry](telemetry/) | [Audit](audit/) |
|---|-----------|-------|
| **Purpose** | Operational monitoring — is the system healthy? | Safety analysis — what did the agent actually do? |
| **When it runs** | Always on in production | On-demand, activated via `AUDIT_ENABLED=1` |
| **What it captures** | Cost (USD), tokens, latency, cache hit rate, error rates, trace waterfall | Which tools were called, with what arguments, on which files, by which agent |
| **Data format** | OTel spans + metrics (industry standard) | JSONL files (one per run, OTel-aligned schema) |
| **Where data goes** | OTel Collector → Tempo (traces), Prometheus (metrics), Langfuse (LLM analytics) | Local JSONL files → aggregation script → audit report |
| **Granularity** | Per-LLM-request: model, tokens, cost, latency | Per-tool-call: tool name, input summary, output summary, agent identity |
| **Key question** | "How much did this run cost and where is the latency?" | "What files did the agent write to and should we restrict that?" |
| **Output** | Grafana dashboards, Langfuse session replay | Audit report → per-agent safety config recommendations |

## When to Use Which

**Use telemetry** for day-to-day operations:
- Monitoring cost trends across pipeline runs
- Debugging slow requests via trace waterfall
- Tracking cache hit rates to optimize prompt caching
- Alerting on error rate spikes

**Use audit** when setting up or changing a pipeline:
- First time running a new pipeline — audit 2-3 unrestricted runs to understand behavior
- After adding new tools or agents — verify they only access expected files/commands
- Deriving safety configs — audit report feeds directly into `safety/` layer configuration
- Compliance — proving what the agent did and didn't do

**In short:** Telemetry tells you how the system is performing. Audit tells you what the system is doing — and whether it should be allowed to.

## Data Contract

The `AuditEvent` dataclass lives in `schemas/audit.py` (shared with the rest of the orchestrator's contracts).

See each subfolder's README for file-level detail:
- [telemetry/README.md](telemetry/README.md) — setup, tracers, backends, attribute matrix
- [audit/README.md](audit/README.md) — hooks, aggregation, output scanning, JSONL schema
