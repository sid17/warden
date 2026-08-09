# Testing OTel & Langfuse Instrumentation

How to verify that both telemetry paths (OTel and Langfuse) are working for both providers after code changes.

> **Setup:** bring up the stack and see run conventions in the [README](./README.md). Model strings in
> pass-criteria are illustrative (current Claude model: `claude-opus-4-8`).

## Prerequisites

| Service | Port | Check |
|---------|------|-------|
| Langfuse v2 | :3456 | `curl -s http://localhost:3456/api/public/health` → 200 |
| OTel Collector | :4317 (OTLP gRPC) | `docker ps --filter name=otel-collector` → `Up` (no HTTP health by default) |
| Ollama | :11434 | `curl -s http://localhost:11434/api/tags` → model list |

Create a project in the Langfuse UI (:3456) and export its keys as `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`:

```bash
export LANGFUSE_PUBLIC_KEY=<your public key>
export LANGFUSE_SECRET_KEY=<your secret key>
export LANGFUSE_HOST=http://localhost:3456
```

## Test 1: OpenHarness — Simple (no tools)

Verifies: `OpenHarnessLangfuseTracer` creates trace + generation, `init_openharness_otel()` runs without error.

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python -c "
import asyncio, time
from warden.providers.openharness.session import OpenHarnessSession
from pathlib import Path

async def main():
    session = OpenHarnessSession(repo_path=Path.cwd(), model='qwen3:1.7b')
    await session.start()
    t0 = time.time()
    async for event in session.send('What is 7 * 8? Just the number.'):
        etype = type(event).__name__
        if etype == 'AssistantTextDelta':
            print(event.text, end='', flush=True)
        elif etype == 'AssistantTurnComplete':
            print(f'  [{time.time()-t0:.1f}s] TurnComplete', flush=True)
    print(f'Session: {session.session_id}', flush=True)
    await session.close()

asyncio.run(main())
"
```

**Verify Langfuse:**

```bash
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=1&orderBy=timestamp.desc" | python3 -c "
import json, sys
t = json.load(sys.stdin)['data'][0]
print(f'Name: {t[\"name\"]}  provider={t[\"metadata\"].get(\"provider\")}  model={t[\"metadata\"].get(\"model\")}  obs={len(t.get(\"observations\", []))}')
"
```

**Pass criteria:**
- [ ] Name = `openharness.interaction`
- [ ] Provider = `openharness`, model = `qwen3:1.7b`
- [ ] 1 observation (generation)
- [ ] Output contains the answer

## Test 2: OpenHarness — Tool call

Verifies: `OpenHarnessLangfuseTracer` creates tool spans from `ToolExecutionStarted`/`Completed`.

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python -c "
import asyncio, time
from warden.providers.openharness.session import OpenHarnessSession
from pathlib import Path

async def main():
    session = OpenHarnessSession(repo_path=Path.cwd(), model='qwen3:1.7b')
    await session.start()
    t0 = time.time()
    async for event in session.send('Read the file CLAUDE.md and tell me the project name in one word.'):
        etype = type(event).__name__
        if etype == 'ToolExecutionStarted':
            print(f'  [{time.time()-t0:.0f}s] ToolStart: {event.tool_name}', flush=True)
        elif etype == 'ToolExecutionCompleted':
            print(f'  [{time.time()-t0:.0f}s] ToolDone: {event.tool_name} error={event.is_error}', flush=True)
        elif etype == 'AssistantTurnComplete':
            print(f'  [{time.time()-t0:.0f}s] TurnComplete', flush=True)
        elif etype == 'AssistantTextDelta':
            print(event.text, end='', flush=True)
    print(f'\nSession: {session.session_id}', flush=True)
    await session.close()

asyncio.run(main())
"
```

**Verify Langfuse:**

```bash
TRACE_ID=$(curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=1&orderBy=timestamp.desc" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])")

curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/observations?traceId=$TRACE_ID&limit=10" | python3 -c "
import json, sys
for obs in sorted(json.load(sys.stdin)['data'], key=lambda x: x['startTime']):
    model = obs.get('model') or ''
    print(f'  {obs[\"type\"]:12} {obs[\"name\"]:20} model={model:15} output={(obs.get(\"output\") or \"\")[:50]}')
"
```

**Pass criteria:**
- [ ] 3 observations: `llm_call_1` (GENERATION), `tool: read_file` (SPAN), `llm_call_2` (GENERATION)
- [ ] Tool span has `output` containing file content
- [ ] `llm_call_1` has empty output (tool call decision turn)
- [ ] `llm_call_2` has the model's answer

## Test 3: Claude — Simple

Verifies: `ClaudeLangfuseTracer` creates trace + generation, `build_claude_otel_env()` provides correct env vars.

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python -c "
import asyncio, time
from warden.providers.claude.session import ClaudeSession
from pathlib import Path

async def main():
    session = ClaudeSession(repo_path=Path.cwd())
    await session.start()
    t0 = time.time()
    async for msg in session.send('What is 7 * 8? Just the number, nothing else.'):
        mtype = type(msg).__name__
        if mtype == 'AssistantMessage':
            for block in (getattr(msg, 'content', None) or []):
                text = getattr(block, 'text', None)
                if text:
                    print(f'  [{time.time()-t0:.1f}s] {text}', flush=True)
        elif mtype == 'ResultMessage':
            print(f'  [{time.time()-t0:.1f}s] cost=\${getattr(msg, \"total_cost_usd\", 0):.4f}', flush=True)
    print(f'Session: {session.session_id}', flush=True)
    await session.close()

asyncio.run(main())
"
```

**Verify Langfuse:**

```bash
curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=1&orderBy=timestamp.desc" | python3 -c "
import json, sys
t = json.load(sys.stdin)['data'][0]
print(f'Name: {t[\"name\"]}  provider={t[\"metadata\"].get(\"provider\")}  model={t[\"metadata\"].get(\"model\")}  cost=\${t[\"metadata\"].get(\"total_cost_usd\", 0):.4f}  obs={len(t.get(\"observations\", []))}')
"
```

**Pass criteria:**
- [ ] Name = `claude_code.interaction`
- [ ] Provider = `claude`, model = `claude-opus-4-8`
- [ ] `total_cost_usd` > 0
- [ ] 1+ observation (generation)

## Test 4: Claude — Full waterfall with sub-agent

Verifies: sub-agent nesting via `parent_tool_use_id`, `TaskStartedMessage`/`TaskNotificationMessage` handling, agent span metadata.

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python warden/scripts/otel-waterfall-test.py
```

**Verify sub-agent nesting:**

```bash
TRACE_ID=$(curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=5&orderBy=timestamp.desc" \
    | python3 -c "import json,sys; [print(t['id']) for t in json.load(sys.stdin)['data'] if 'Agent tool' in t['input']]" | head -1)

curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/observations?traceId=$TRACE_ID&limit=20" | python3 -c "
import json, sys
for obs in sorted(json.load(sys.stdin)['data'], key=lambda x: x['startTime']):
    pid = obs.get('parentObservationId')
    parent = 'ROOT' if not pid else pid[:12]
    model = obs.get('model') or ''
    print(f'  {obs[\"type\"]:12} parent={parent:12} {obs[\"name\"]:20} model={model}')
"
```

**Pass criteria:**
- [ ] 5 traces in session (one per turn)
- [ ] Turn 4 trace has 6 observations
- [ ] `llm_call_3` (Haiku) has `parentObservationId` pointing to the Agent span
- [ ] `tool: Grep` has `parentObservationId` pointing to the Agent span
- [ ] Agent span has metadata: `subagent_type`, `status=completed`, `total_tokens > 0`

## Test 5: OpenHarness — Agent span enrichment

Verifies: Agent tool spans are held open until the subprocess completes, then enriched with real output, status, and duration via `BackgroundTaskManager` completion listener.

```bash
PYTHONPATH=. LANGFUSE_HOST=http://localhost:3456 \
    .venv/bin/python warden/scripts/openharness-agent-trace-test.py
```

**Verify Langfuse:**

```bash
TRACE_ID=$(curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/traces?limit=1&orderBy=timestamp.desc" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])")

curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
    "http://localhost:3456/api/public/observations?traceId=$TRACE_ID&limit=20" | python3 -c "
import json, sys
for obs in sorted(json.load(sys.stdin)['data'], key=lambda x: x['startTime']):
    meta = obs.get('metadata') or {}
    status = meta.get('status', '')
    duration = meta.get('duration_ms', '')
    print(f'  {obs[\"type\"]:12} {obs[\"name\"]:25} status={status:12} duration_ms={duration}')
"
```

**Pass criteria:**
- [ ] Agent span has `output` containing actual subprocess result (not "Spawned agent...")
- [ ] Agent span `metadata` includes `status` = `completed` (or `failed`/`killed`)
- [ ] Agent span `metadata` includes `duration_ms` > 0
- [ ] Non-agent tool spans still close normally (Test 2 does not regress)

## What each test validates

| Test | OTel config | Langfuse tracer | Tool spans | Sub-agent nesting |
|------|------------|----------------|------------|------------------|
| 1. OH simple | `init_openharness_otel()` | `OpenHarnessLangfuseTracer` | — | — |
| 2. OH tool call | same | same | `ToolExecutionStarted/Completed` → span | — |
| 3. Claude simple | `build_claude_otel_env()` | `ClaudeLangfuseTracer` | — | — |
| 4. Claude waterfall | same | same | `ToolUseBlock` → span | `parent_tool_use_id` routing |
| 5. OH agent span | same | same | Agent span held open → enriched via completion listener | Flat (subprocess output only) |
