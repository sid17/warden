# Harness Observability — guides index & how it all comes together

How the harness makes an agent run *visible*: two telemetry paths off every provider session, the
stack that receives them, the reference taxonomy they should produce, and the runbooks to verify and
regenerate that baseline.

> **Scope:** this folder is the observability *how-to* — the runbooks to verify and
> regenerate the telemetry baseline. For the conceptual model, see
> [`../../02-observability.md`](../../02-observability.md).
>
> **Note on the stack.** These runbooks reference a reference deployment — an OTEL
> Collector + Tempo + Prometheus + Grafana + Langfuse v2, driven by
> `infra/docker-compose.*.yml` and `infra/observability/otel-collector-config.yaml`.
> **That compose stack is not bundled in this repo** — stand up your own equivalent (or
> any OTLP-compatible backend) and adapt the endpoints/paths in the commands below. The
> `phoenix_` Prometheus metric prefix is just the collector's configured `namespace` — set
> it to whatever you like.

## The one-paragraph model

Every provider session emits **two independent signals**. **(1) OTEL** — Claude Code exports natively
(just pass it the env), OpenHarness via the OpenLLMetry instrumentor — flows to the **OTEL Collector**,
which fans out to **Tempo** (trace waterfalls) and **Prometheus** (spanmetrics), both surfaced in
**Grafana**. **(2) Langfuse** — the hand-written per-provider tracers push traces/generations/spans
directly via the Langfuse SDK for **LLM analytics** (prompt/output/cost/session replay + sub-agent
nesting). *Grafana answers "how is the fleet doing?"; Langfuse answers "what did this turn say and
cost?"*

```
provider session ─┬─ native OTEL / OpenLLMetry ─▶ Collector :4317 ─┬─▶ Tempo :3200 ──┐
                  │                                                └─▶ Prometheus :9090 ─┴─▶ Grafana :3030
                  └─ Langfuse SDK ───────────────────────────────────▶ Langfuse v2 :3456
```

## Bring the stack up

```bash
cd "$(git rev-parse --show-toplevel)"
# -p harness names the compose project; keep it consistent across both files and
# the container names below.
docker compose -p harness -f infra/docker-compose.langfuse.yml      up -d
docker compose -p harness -f infra/docker-compose.observability.yml up -d
```

| Service | URL | Health check |
|---|---|---|
| Langfuse v2 | http://localhost:3456 | `curl -s -o /dev/null -w '%{http_code}' localhost:3456/api/public/health` → 200 |
| OTEL Collector | gRPC :4317 | (spans arrive here; no HTTP health by default) |
| Tempo | http://localhost:3200 | `/ready` → 200 |
| Prometheus | http://localhost:9090 | `/-/ready` → 200 |
| Grafana | http://localhost:3030 | `/api/health` → 200 |
| Ollama (OpenHarness) | http://localhost:11434 | `/api/tags` → model list |

Langfuse keys for the CLI/SDK: create a project in the Langfuse UI (:3456) and export its keys as
`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`, then set
`LANGFUSE_HOST=http://localhost:3456`. The Python `langfuse` SDK (`<3`, v2 API) must be installed in the
venv for the Langfuse path; the OTEL path needs no Python dep (env vars → the CLI subprocess).

## The guides

| Doc | Use it to… |
|---|---|
| **[establishing-a-baseline.md](./establishing-a-baseline.md)** | **Capture a fresh events baseline end-to-end** — bring up the stack, fire the 3 canonical scenarios (no-tool / single-tool / sub-agent) on Claude, and read back both paths. Start here. |
| [baseline-taxonomy.md](./baseline-taxonomy.md) | The **reference taxonomy** — the exact spans/observations/attributes each provider should produce (Claude-native is the anchor) + the parity-target table providers build toward. |
| [testing-instrumentation.md](./testing-instrumentation.md) | Verify **both paths work for both providers** after a code change (5 focused smoke tests). |
| [otel-smoke-test.md](./otel-smoke-test.md) | Verify **OTEL spans reach Tempo** and **spanmetrics reach Prometheus**. |
| [langfuse-smoke-test.md](./langfuse-smoke-test.md) | Verify the **Langfuse trace hierarchy + sub-agent nesting + cost** in detail. |
| [reports/](../../../observability/telemetry/reports/) | Dated capture results (`2026-06-22-*`, `2026-07-21-baseline-3-scenarios.md`). |

## Conventions

- **Run Python** via `.venv/bin/python` with `PYTHONPATH=.` from the repo root.
- **Provider imports** are `warden.providers.<name>.session`; the telemetry code lives in
  `warden/observability/telemetry/`.
- **Never bill** — Claude/Codex use OAuth, OpenHarness uses free local Ollama (`qwen3:8b`). Prefix runs
  with `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY` so a stray key can't switch you to the paid lane.
- **Tempo TraceQL** uses the resource scope: `{resource.service.name="claude-agent"}` (the bare
  `service.name` form is rejected by the search API).

> **Note:** the OTEL collector config exports traces to Tempo *and* an `otlphttp/opik` backend at
> `:5174` that isn't running — you'll see harmless retry logs. Tempo is independent and works.
