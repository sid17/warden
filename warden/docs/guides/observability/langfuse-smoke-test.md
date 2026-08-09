# Langfuse Trace Smoke Test

Validates that the Langfuse SDK produces correct trace hierarchy, sub-agent nesting, metadata, and token counts.

## Prerequisites

- Langfuse v2 running on `:3456`. Create a project in the Langfuse UI and export its keys as
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (used by every command below).
- See [README.md](README.md) for full prerequisites

## Step 1: Run the test script

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python warden/scripts/otel-waterfall-test.py
```

Wait for all 5 turns to complete. Note the session ID printed at the end.

## Step 2: Verify traces landed

```bash
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=5&orderBy=timestamp.desc" \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)['data']
for t in data:
    obs = len(t.get('observations', []))
    print(f\"  {t['input'][:60]:60}  obs={obs}  session={t.get('sessionId', 'none')[:12]}\")
"
```

**Pass criteria:**
- 5 traces visible, all with the same `sessionId`
- Turn 1 (Read): 2+ observations
- Turn 4 (Agent): 4+ observations (main LLM calls + agent span + sub-agent children)
- Turn 5 (Summary): 1 observation (no tools)

## Step 3: Verify sub-agent nesting (turn 4)

Find the Agent trace and inspect its observation hierarchy:

```bash
# Get the Agent trace ID
TRACE_ID=$(curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=5&orderBy=timestamp.desc" \
    | python3 -c "import json,sys; [print(t['id']) for t in json.load(sys.stdin)['data'] if 'Agent tool' in t['input']]")

# List observations with nesting
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/observations?traceId=$TRACE_ID&limit=20" \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)['data']
for obs in sorted(data, key=lambda x: x['startTime']):
    pid = obs.get('parentObservationId')
    parent = 'ROOT' if not pid else pid[:12]
    model = obs.get('model') or ''
    print(f\"  {obs['type']:12} {obs['id'][:12]}  parent={parent:12}  {obs['name']:20}  model={model}\")
"
```

**Pass criteria — expected hierarchy:**

```
GENERATION   ...  parent=ROOT          llm_call_1              model=claude-opus-4-8
GENERATION   ...  parent=ROOT          llm_call_2              model=claude-opus-4-8
SPAN         ...  parent=ROOT          tool: Agent             model=
GENERATION   ...  parent=<agent_span>  llm_call_3              model=claude-haiku-4-5-20251001
SPAN         ...  parent=<agent_span>  tool: Grep              model=
GENERATION   ...  parent=ROOT          llm_call_4              model=claude-opus-4-8
```

Key checks:
- [ ] Sub-agent generation (`llm_call_3`) is a **child** of `tool: Agent`, not ROOT
- [ ] Sub-agent tool (`tool: Grep`) is a **child** of `tool: Agent`, not ROOT
- [ ] Sub-agent generations carry **their own model** (may be Haiku or Opus, per the sub-agent config — a recent Explore run used Opus + `Bash`)
- [ ] `tool: Agent` span has `startTime` and `endTime` (not null)

## Step 4: Verify Agent span metadata

```bash
# Get the Agent span ID from the previous output, then:
AGENT_OBS_ID=<paste agent span id>

curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/observations/$AGENT_OBS_ID" \
    | python3 -c "
import json, sys
obs = json.load(sys.stdin)
meta = obs.get('metadata', {})
print('Metadata:')
for k, v in sorted(meta.items()):
    print(f'  {k}: {v}')
print(f'Input: {json.dumps(obs.get(\"input\", {}))[:200]}')
print(f'Output: (obs.get(\"output\") or \"\")[:200]')
"
```

**Pass criteria:**
- [ ] `subagent_type` present (e.g., `Explore`)
- [ ] `status` = `completed`
- [ ] `task_id` present
- [ ] `total_tokens` > 0
- [ ] `duration_ms` > 0
- [ ] `input` contains the sub-agent prompt
- [ ] `output` contains the sub-agent result (truncated per `LANGFUSE_TOOL_OUTPUT_LIMIT`)

## Step 5: Verify trace-level metadata

```bash
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces/$TRACE_ID" \
    | python3 -c "
import json, sys
t = json.load(sys.stdin)
meta = t.get('metadata', {})
print('Trace metadata:')
for k, v in sorted(meta.items()):
    if k == 'model_usage':
        print(f'  model_usage:')
        for model, usage in v.items():
            print(f'    {model}: {json.dumps(usage)}')
    else:
        print(f'  {k}: {v}')
"
```

**Pass criteria:**
- [ ] `model_usage` contains per-model breakdown (Opus + Haiku)
- [ ] `total_cost_usd` > 0
- [ ] `duration_ms` > 0
- [ ] `num_turns` >= 1
- [ ] `llm_calls` >= 1

## Step 6: Verify tool output truncation

```bash
# Re-run with output suppressed
LANGFUSE_TOOL_OUTPUT_LIMIT=0 PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python warden/scripts/otel-waterfall-test.py
```

**Pass criteria:**
- [ ] Tool span outputs show `(captured, output suppressed)` or `(no output)` instead of actual content

Optional: test with `LANGFUSE_TOOL_OUTPUT_LIMIT=-1` for full output.

## Report template

After completing all steps, create a report at `reports/YYYY-MM-DD-<phase>-results.md` with:

1. Date and phase
2. Pass/fail for each step
3. Observation hierarchy (copy the output from Step 3)
4. Agent span metadata (copy from Step 4)
5. Any anomalies or failures observed
6. Screenshot of Langfuse Timeline view (optional but helpful)
