# OTel Infra Smoke Test

Validates that OTel spans reach Tempo and spanmetrics reach Prometheus via the collector.

## Prerequisites

- OTel Collector, Tempo, Prometheus, and Grafana running
- See [README.md](README.md) for full prerequisites and ports

## Step 1: Run the test script

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python warden/scripts/otel-waterfall-test.py
```

Wait for all 5 turns to complete.

## Step 2: Verify spans in Tempo

Query Tempo for Claude agent spans:

```bash
# Via Tempo API (TraceQL needs the resource scope; the bare service.name form is rejected)
curl -s -G "http://localhost:3200/api/search" \
    --data-urlencode 'q={resource.service.name="claude-agent"}' --data-urlencode "limit=5" \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
traces = data.get('traces', [])
print(f'Traces found: {len(traces)}')
for t in traces[:5]:
    spans = t.get('spanSets', [{}])[0].get('matched', 0) if t.get('spanSets') else 0
    print(f'  traceID={t[\"traceID\"][:16]}  rootService={t.get(\"rootServiceName\", \"?\")}  spans={spans}')
"
```

**Pass criteria:**
- [ ] At least 1 trace found with `service.name=claude-agent`
- [ ] Root service name is `claude-agent`

Alternative: Open Grafana at `http://localhost:3030` → Explore → Tempo → query `{resource.service.name="claude-agent"}` and visually inspect the waterfall.

## Step 3: Verify span structure in Tempo

Pick a trace ID from Step 2 and inspect its spans:

```bash
TEMPO_TRACE_ID=<paste trace ID from step 2>

curl -s "http://localhost:3200/api/traces/$TEMPO_TRACE_ID" \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
batches = data.get('batches', [])
for batch in batches:
    for span in batch.get('scopeSpans', [{}])[0].get('spans', []):
        name = span.get('name', '?')
        attrs = {a['key']: a.get('value', {}).get('stringValue', a.get('value', {}).get('intValue', '?'))
                 for a in span.get('attributes', [])}
        model = attrs.get('gen_ai.request.model', '')
        print(f'  {name:40}  model={model}')
"
```

**Pass criteria:**
- [ ] Root span: `claude_code.interaction` with `session.id` attribute
- [ ] Child spans: `claude_code.llm_request` with `gen_ai.request.model` and `gen_ai.system=anthropic`
- [ ] Token attributes present: `input_tokens`, `output_tokens`

## Step 4: Verify Prometheus spanmetrics

```bash
# Total interaction count. The phoenix_ prefix comes from the OTel collector's configured
# namespace (see infra/observability/otel-collector-config.yaml); adjust if you change it.
curl -s "http://localhost:9090/api/v1/query?query=phoenix_traces_span_metrics_calls_total" \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
results = data.get('data', {}).get('result', [])
for r in results:
    labels = r['metric']
    name = labels.get('span_name', '?')
    model = labels.get('gen_ai_request_model', '')
    value = r['value'][1]
    print(f'  {name:40}  model={model:30}  count={value}')
"
```

**Pass criteria:**
- [ ] `claude_code.interaction` count > 0
- [ ] `claude_code.llm_request` with `gen_ai_request_model=claude-opus-4-8[1m]` count > 0
- [ ] `claude_code.llm_request` with `gen_ai_request_model=claude-haiku-4-5-20251001` count > 0

Also expect tool spans: `claude_code.tool`, `claude_code.tool.execution`, `claude_code.tool.blocked_on_user`.

## Step 5: Verify collector is receiving spans

The default collector config does not expose the `:13133` health endpoint, so confirm liveness by the
data itself (Step 4 above) and the container state:

```bash
docker ps --filter name=otel-collector --format '{{.Names}} {{.Status}}'
# container is named <project>-otel-collector-1 (here harness-otel-collector-1, per -p harness)
docker logs harness-otel-collector-1 2>&1 | grep -i "Everything is ready" | tail -1
```

**Pass criteria:**
- [ ] Collector container is `Up`
- [ ] Spanmetrics appear in Step 4 (the real proof spans are flowing)

> The collector also exports to an `otlphttp/opik` backend (`:5174`) that isn't running — harmless
> retry logs; Tempo is a separate exporter and works.

## Step 6: OpenHarness provider (optional)

If an Ollama model with tool support is available (e.g., `qwen3:8b`):

```bash
PYTHONPATH=. .venv/bin/python -m warden.drive.cli \
    --provider openharness --model qwen3:8b --single "What is 2+2? Just answer."
```

Then verify:
```bash
# Tempo query for OpenHarness
curl -s -G "http://localhost:3200/api/search" \
    --data-urlencode 'q={resource.service.name="openharness"}' --data-urlencode "limit=5"
```

**Pass criteria:**
- [ ] `openai.chat` span visible with `gen_ai.request.model` attribute
- [ ] Prometheus: `phoenix_traces_span_metrics_calls_total{span_name="openai.chat"}` > 0

## Report template

After completing all steps, create a report at `reports/YYYY-MM-DD-<phase>-results.md` with:

1. Date and phase
2. Pass/fail for each step
3. Tempo span structure (copy from Step 3)
4. Prometheus metric values (copy from Step 4)
5. Any anomalies or failures observed
