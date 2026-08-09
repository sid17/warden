# Establishing an observability events baseline

> **What this is.** The end-to-end runbook for capturing a **fresh telemetry baseline** — the exact
> events, spans, and cost a real Claude run produces on **both** paths (Langfuse + OTEL/Tempo/Prometheus).
> Run it after a provider/telemetry change, on a new model, or whenever you need ground truth to compare
> against. The *reference shape* to compare against is [`baseline-taxonomy.md`](./baseline-taxonomy.md);
> this doc is how you *produce* a capture. **Cost:** Claude via **OAuth = $0 marginal** — never the API-key lane.

## Why a baseline (and why these 3 scenarios)

A baseline is a *known-good snapshot* of "what one query emits," so you can (a) prove the stack works,
(b) diff after a change, and (c) measure provider parity. Three scenarios span the shapes that matter:

| Scenario | Prompt shape | What it isolates |
|---|---|---|
| **A — no tools** | pure arithmetic ("7×8") | the minimal turn: `interaction` + `llm_request` only, no tool spans |
| **B — single tool** | "read a file" | one tool span, no delegation |
| **C — sub-agent** | "use the Agent tool to search…" | **the hard case** — sub-agent LLM + tool calls nested under `tool: Agent` |

One with a sub-agent and one without is the load-bearing pair: the sub-agent case is where providers
*diverge* (Claude nests it; OpenHarness loses it across the subprocess boundary — the sub-agent-internals gap).

## 0. Preflight

```bash
cd "$(git rev-parse --show-toplevel)"
docker version >/dev/null && echo "docker up"
security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
  | python3 -c 'import json,sys; print("claude oauth:", "yes" if json.load(sys.stdin).get("claudeAiOauth",{}).get("accessToken") else "no")'
claude --version   # the SDK spawns this CLI; it uses the Keychain OAuth token
# Langfuse SDK (v2 API) for the Langfuse path — the OTEL path needs no python dep:
.venv/bin/python -c "import langfuse; print(langfuse.__version__)" 2>/dev/null || uv pip install 'langfuse<3'
```

## 1. Bring up the stack

```bash
docker compose -p harness -f infra/docker-compose.langfuse.yml      up -d
docker compose -p harness -f infra/docker-compose.observability.yml up -d
# wait for Langfuse migrations (~10-30s), then confirm health + keys:
for i in $(seq 1 18); do [ "$(curl -s -o /dev/null -w '%{http_code}' localhost:3456/api/public/health)" = 200 ] && break; sleep 5; done
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" "http://localhost:3456/api/public/traces?limit=1" | head -c 60; echo
```

> **Langfuse keys.** Create a project in the Langfuse UI at :3456 and export its keys as
> `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (and `LANGFUSE_HOST=http://localhost:3456`). Keep the
> compose project name consistent (`-p harness`) across both files and the container names below.

## 2. Fire the scenarios

The high-level `ChatAPI` wires OTEL (auto) + the Langfuse tracer (env-gated) + the Agent tool. Run each
scenario in its **own session** for clean, comparable traces. A ready driver:

```bash
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
  LANGFUSE_HOST=http://localhost:3456 \
  .venv/bin/python warden/scripts/otel-waterfall-test.py
```

`warden/scripts/otel-waterfall-test.py` runs a 5-turn session (Read · WebSearch · Glob+Read · **Agent
sub-agent** · summary) — it covers B and C in one session. For clean *per-scenario* sessions (A/B/C
isolated), use a driver that calls `ChatAPI(HarnessConfig(), repo_path=".")` → `api.init()` →
`async for ev in api.send(prompt, workflow=None)` once per scenario (see the 2026-07-21 report for the
exact 3-scenario script). Key API notes:

- **`env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY`** — strip API keys so nothing accidentally bills; Claude
  uses the Keychain OAuth token via the CLI.
- The driver's printed `session_id` may be `None` (it reads `_current_session_id`) — **cosmetic**; the
  real session id is on every span/trace.

## 3. Read back Path 1 — Langfuse (content + cost + nesting)

```bash
# recent traces: name / observation count / cost / model
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" "http://localhost:3456/api/public/traces?limit=5&orderBy=timestamp.desc" \
 | python3 -c 'import json,sys
for t in json.load(sys.stdin)["data"]:
    m=t.get("metadata") or {}
    print(t.get("name"), "obs="+str(len(t.get("observations",[]))), "cost=$%.4f"%m.get("total_cost_usd",0), m.get("model"))'
```
Then, for the sub-agent trace, list observations sorted by start time and print `parentObservationId` to
see the nesting (llm_call_N + tool spans under `tool: Agent`). The full query is in
[`langfuse-smoke-test.md`](./langfuse-smoke-test.md) Step 3; a self-contained version is in the
2026-07-21 report's helper script.

**What good looks like (Langfuse):**
- 3 traces named `claude_code.interaction`; A obs≈1, B obs≈4, C obs≈11.
- **Sub-agent nesting (C):** `llm_call_*` and `tool: Bash/Grep` nested under `tool: Agent`, then back to
  ROOT. The `tool: Agent` span carries `subagent_type`, `status=completed`, `total_tokens`, `duration_ms`.
- Trace-level `model_usage` shows a **per-model cost breakdown** (Opus response + Haiku classification).

## 4. Read back Path 2 — OTEL → Tempo + Prometheus (shape + metrics)

```bash
# Tempo: the trace list (tag search works reliably; TraceQL {} needs a time range)
curl -s -G "http://localhost:3200/api/search" --data-urlencode "tags=service.name=claude-agent" --data-urlencode "limit=5" \
 | python3 -c 'import json,sys; [print(t["traceID"][:16], t.get("rootTraceName")) for t in json.load(sys.stdin).get("traces",[])]'

# Tempo: span structure of a trace (paste an id) — names + gen_ai.* attributes
curl -s "http://localhost:3200/api/traces/<TRACE_ID>" | python3 -c 'import json,sys
from collections import Counter
c=Counter()
for b in json.load(sys.stdin).get("batches",[]):
  for ss in b.get("scopeSpans",[]):
    for s in ss.get("spans",[]): c[s["name"]]+=1
print(dict(c))'

# Prometheus: spanmetrics (proves the whole pipeline; same data feeds Tempo)
# the phoenix_ prefix comes from the OTel collector's configured namespace
# (see infra/observability/otel-collector-config.yaml); adjust if you change it.
curl -s -G "http://localhost:9090/api/v1/query" --data-urlencode "query=phoenix_traces_span_metrics_calls_total" \
 | python3 -c 'import json,sys
for r in json.load(sys.stdin)["data"]["result"]:
    m=r["metric"]; print(m.get("span_name"), m.get("gen_ai_request_model",""), "=", r["value"][1])'
```

**What good looks like (OTEL):**
- **Tempo** — the sub-agent trace ≈ 20 spans: `claude_code.interaction` (root, `session.id`) + N×
  `claude_code.llm_request` (`gen_ai.request.model`, `gen_ai.system=anthropic`, `input_tokens`,
  `output_tokens`, `cache_read_tokens`) + tool spans (`claude_code.tool` / `.tool.execution` /
  `.tool.blocked_on_user`).
- **Prometheus** — `claude_code.interaction` = (#scenarios), `claude_code.llm_request` split by model
  (haiku classification + opus response), `claude_code.tool*` counts. If empty, wait ~30 s (delta
  temporality + scrape interval) and retry.
- **Grafana** (http://localhost:3030 → Explore → Tempo → `{resource.service.name="claude-agent"}`) renders the
  waterfall visually.

## 5. Record the baseline

Write a dated report to `warden/observability/telemetry/reports/YYYY-MM-DD-<label>.md` with:
the scenarios + answers, the Langfuse trace/obs/cost table + the sub-agent nesting, the Tempo span
breakdown + Prometheus spanmetrics, and any drift from [`baseline-taxonomy.md`](./baseline-taxonomy.md)
(if the *shape* changed, update that doc's §3–§6). The 2026-07-21 report is the template.

## Gotchas (learned live 2026-07-21)

- **Consistent project name** — use the same `-p harness` across both compose files (§1).
- **`opik` exporter noise** — the collector config exports traces to both Tempo *and* a non-running
  `otlphttp/opik` backend (`:5174`) → constant retry logs. Harmless; Tempo works. (Cleanup: drop the
  opik exporter.)
- **`langfuse` not in the workspace** — install `langfuse<3` (v2 `.trace()`/`ModelUsage` API) into
  `.venv`; the OTEL path needs no Python dep.
- **Import path** — `warden.providers.<name>.session` (not `orchestrator.providers.*`).
- **Cost isn't monotonic with complexity** — a cold-cache first turn pays cache-*creation* and can cost
  more than a later, more complex turn that reuses cache. Read `model_usage` cacheRead vs cacheCreate,
  not the headline `$`.
- **OTEL redacts prompts** by default (`OTEL_LOG_USER_PROMPTS=true` to log); **Langfuse always has full
  content**. Grafana for shape, Langfuse for content.

## Tear down

```bash
docker compose -p harness -f infra/docker-compose.observability.yml down
docker compose -p harness -f infra/docker-compose.langfuse.yml down   # add -v to also wipe the Langfuse DB volume
```
