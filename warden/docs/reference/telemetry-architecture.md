# Observability Architecture: How Telemetry Works

> How the harness captures telemetry for Claude and OpenHarness providers, what data flows through each path, and why the two providers have fundamentally different observability capabilities.

## Two Telemetry Paths

Every provider has two independent telemetry paths. They serve different purposes and know nothing about each other.

```
                    ┌──────────────────────────────┐
                    │     Provider (Claude or OH)   │
                    └──────────┬───────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                                 ▼
    Path 1: OTel                      Path 2: Langfuse SDK
    (automatic / native)              (manual, our code)
              │                                 │
              ▼                                 ▼
    OTel Collector (:4317)            Langfuse v2 (:3456)
       │         │                              │
       ▼         ▼                              ▼
    Tempo    Prometheus               LLM Analytics
  (traces)  (metrics)               (sessions, cost,
                                     tool spans, agents)
```

**Path 1 (OTel)** captures raw infrastructure telemetry — spans for every API call, with model names, token counts, and latency. This feeds Tempo waterfalls and Prometheus metrics dashboards.

**Path 2 (Langfuse)** captures semantic LLM analytics — what the agent did, which tools it used, what it produced, how much it cost. This is what we build manually by iterating the provider's event stream.

---

## Claude Provider: OTel Path

### How it works

The Claude Agent SDK has **native OTel support**. When we set `CLAUDE_CODE_ENABLE_TELEMETRY=1` and point `OTEL_EXPORTER_OTLP_ENDPOINT` at the collector, the SDK emits spans automatically. We don't instrument anything — the SDK does it internally.

```python
# In ClaudeSession.start() — we just set env vars
options.env = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
    "OTEL_SERVICE_NAME": "claude-agent",
    ...
}
```

### What spans are emitted

The SDK emits a **two-level hierarchy** per interaction:

```
claude_code.interaction                    ← root span (one per send())
├── claude_code.llm_request (Haiku)        ← system prompt classification
├── claude_code.llm_request (Opus)         ← main LLM response
├── claude_code.llm_request (Opus)         ← response after tool use
└── ...
```

### Span data structure

**Root span: `claude_code.interaction`**

| Attribute | Type | Example | Notes |
|-----------|------|---------|-------|
| `session.id` | string | `03cd48c7-9acc-...` | SDK session ID |
| `span.type` | string | `interaction` | Always "interaction" |
| `user_prompt` | string | `<REDACTED>` | Redacted unless `OTEL_LOG_USER_PROMPTS=true` |
| `user_prompt_length` | int | `30` | Character count |
| `interaction.sequence` | int | `1` | Turn number within session |
| `interaction.duration_ms` | int | `2657` | Wall clock time |

**Child span: `claude_code.llm_request`**

| Attribute | Type | Example | Notes |
|-----------|------|---------|-------|
| `gen_ai.system` | string | `anthropic` | Always "anthropic" |
| `gen_ai.request.model` | string | `claude-opus-4-7[1m]` | Full model identifier |
| `input_tokens` | int | `6` | Prompt tokens |
| `output_tokens` | int | `248` | Completion tokens |
| `cache_read_tokens` | int | `21204` | Prompt cache hits |
| `gen_ai.response.finish_reasons` | string[] | `["end_turn"]` | Why model stopped |

### What we DON'T control

- Span names — the SDK chooses them
- Attribute names — SDK uses its own conventions (not fully GenAI semconv-compliant)
- Granularity — SDK decides what constitutes a span
- Tool call spans — the SDK does NOT emit separate spans for tool executions

---

## OpenHarness Provider: OTel Path

### How it works

OpenHarness has **no native OTel support**. We add it ourselves using OpenLLMetry's `OpenAIInstrumentor`, which monkey-patches the `openai` Python library in our process.

```python
# In observability/telemetry/setup.py::init_openharness_otel(cfg) —
# we instrument the process ourselves
from opentelemetry.instrumentation.openai import OpenAIInstrumentor

provider = TracerProvider(resource=Resource.create({"service.name": "openharness"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(...)))
trace.set_tracer_provider(provider)

OpenAIInstrumentor().instrument()  # patches openai library in THIS process
```

`OpenAIInstrumentor` is optional — if OpenLLMetry isn't installed, LLM-call
auto-spans are simply off and manual tracing still works.

After this call, every `openai.chat.completions.create()` in the current process automatically creates an OTel span. Since OpenHarness uses `OpenAICompatibleClient` (which wraps the `openai` library) for all LLM calls, every LLM call gets traced.

### What spans are emitted

OpenLLMetry emits **one span per API call** — there's no parent interaction span:

```
openai.chat                               ← one per LLM call (flat, no hierarchy)
openai.chat                               ← next LLM call (separate trace)
```

### Span data structure

**Span: `openai.chat`**

| Attribute | Type | Example | Notes |
|-----------|------|---------|-------|
| `gen_ai.operation.name` | string | `chat` | Always "chat" |
| `gen_ai.provider.name` | string | `openai` | Protocol, not actual provider |
| `gen_ai.request.model` | string | `qwen3:1.7b` | Model name from Ollama |
| `gen_ai.request.max_tokens` | int | `4096` | Max tokens setting |
| `gen_ai.is_streaming` | bool | `true` | Streaming mode |
| `gen_ai.openai.api_base` | string | `http://localhost:11434/v1/` | Ollama endpoint |
| `gen_ai.input.messages` | string | `[{role: "user", ...}]` | Full message array |
| `gen_ai.tool.definitions` | string | `[{type: "function", ...}]` | Tool schemas |
| `gen_ai.usage.input_tokens` | int | `0` | Often 0 (Ollama streaming) |
| `gen_ai.usage.output_tokens` | int | `0` | Often 0 (Ollama streaming) |
| `error.type` | string | `BadRequestError` | On failure |

### What we DON'T get

- No interaction-level span wrapping multiple LLM calls
- No session ID correlation
- Token counts are 0 from Ollama in streaming mode
- No tool execution spans (tools are outside the OpenAI API call)
- No cost tracking

---

## Claude Provider: Langfuse Path

### How it works

We iterate the Claude SDK's message stream and manually create Langfuse observations. The logic lives in `observability/telemetry/claude_langfuse_tracer.py`.

```python
# In ClaudeSession.send()
tracer = ClaudeLangfuseTracer.create(session_id, prompt)
async for msg in client.receive_response():
    tracer.handle_message(msg)  # creates Langfuse observations
tracer.finalize()
```

`ClaudeLangfuseTracer.handle_message()` routes each SDK message type to create the appropriate Langfuse observation:

| SDK Message | Langfuse Observation |
|-------------|---------------------|
| `AssistantMessage` | `trace.generation()` with model, tokens, output |
| `AssistantMessage` with `ToolUseBlock(name="Agent")` | `trace.span("tool: Agent")` — nesting container |
| `AssistantMessage` with `ToolUseBlock` (other) | `trace.span("tool: {name}")` |
| `AssistantMessage` with `parent_tool_use_id` set | `agent_span.generation()` — nested under agent |
| `UserMessage` | Closes open tool span with result output |
| `UserMessage` with `tool_use_result` | Closes agent span with sub-agent result |
| `TaskStartedMessage` | Enriches agent span metadata (task_id, subagent_type) |
| `TaskNotificationMessage` | Enriches agent span metadata (status, tokens, duration) |
| `ResultMessage` | Updates trace with cost, model_usage, duration |

### Langfuse trace structure

```
Trace: claude_code.interaction
│   session_id, input, output, metadata(cost, model_usage, duration)
│
├── Generation: llm_call_1
│     model=claude-opus-4-7, tokens, stop_reason, message_id
│
├── Span: tool: Read
│     input={file_path: "..."}, output="file contents..."
│
├── Generation: llm_call_2
│     model=claude-opus-4-7, tokens
│
├── Span: tool: Agent                    ← nesting container
│   │ input={prompt, subagent_type}, output=sub-agent result
│   │ metadata: task_id, status, duration_ms, total_tokens
│   │
│   ├── Generation: llm_call_3           ← sub-agent LLM call
│   │     model=claude-haiku-4-5, tokens, parent_tool_use_id
│   │
│   └── Span: tool: Grep                ← sub-agent tool call
│         input={pattern: "..."}, output="file list..."
│
└── Generation: llm_call_4
      model=claude-opus-4-7, tokens
```

### Why sub-agent nesting works

The Claude SDK **multiplexes** all messages — parent and sub-agent — onto a single async iterator. Sub-agent messages carry `parent_tool_use_id` which links them to the parent's `ToolUseBlock`. Our tracer uses this as a routing key:

```
parent_tool_use_id = null     → nest under trace root
parent_tool_use_id = "toolu_018b..."  → nest under agent_spans["toolu_018b..."]
```

Everything runs in one process, one event stream. We see every message from every agent.

---

## OpenHarness Provider: Langfuse Path

### How it works

We iterate the `QueryEngine`'s `StreamEvent` iterator and manually create Langfuse observations. The logic is extracted to `observability/telemetry/openharness_langfuse_tracer.py` (`OpenHarnessLangfuseTracer`), driven from `providers/openharness/session.py`.

```python
# In OpenHarnessSession.send()
lf_trace = lf.trace(name="openharness.interaction", ...)
async for event in engine.submit_message(prompt):
    if event is AssistantTurnComplete:
        lf_trace.generation(name=f"llm_call_{turn}", ...)
    elif event is ToolExecutionStarted:
        open_tool_span = lf_trace.span(name=f"tool: {tool_name}", ...)
    elif event is ToolExecutionCompleted:
        open_tool_span.end(output=tool_output)
```

| StreamEvent | Langfuse Observation |
|-------------|---------------------|
| `AssistantTurnComplete` | `trace.generation()` with model, turn count |
| `ToolExecutionStarted` | `trace.span("tool: {name}")` with input |
| `ToolExecutionCompleted` | Closes span with output, is_error |

### Langfuse trace structure

```
Trace: openharness.interaction
│   session_id, input, output, metadata(model, llm_calls)
│
├── Generation: llm_call_1
│     model=qwen3:1.7b, tokens=0/0 (Ollama streaming)
│
├── Span: tool: read_file
│     input={path: "..."}, output="file contents..."
│
└── Generation: llm_call_2
      model=qwen3:1.7b, tokens=0/0
```

For agent calls:

```
Trace: openharness.interaction
│
├── Generation: llm_call_1
│
├── Span: tool: agent
│     input={prompt, subagent_type}
│     output="Spawned agent agent@default (task_id=a871a9eae)"
│                  ↑ spawn confirmation only, NOT the agent's result
│
└── Generation: llm_call_2
      output="The agent has been spawned..."  ← placeholder, not real result
```

### Why sub-agent nesting does NOT work

The sub-agent runs as a **separate subprocess** (`python -m openharness --task-worker`). It has its own Python process, its own `QueryEngine`, its own event stream. Nobody iterates that stream to create Langfuse observations.

```
Our process (instrumented)          Subprocess (NOT instrumented)
│                                   │
│ We iterate events                 │ Events rendered to stdout as text
│ We create Langfuse observations   │ No Langfuse code running
│ We have parent span context       │ No parent span context
│                                   │
│ openai lib is monkey-patched      │ openai lib is NOT patched
│ (OpenAIInstrumentor ran here)     │ (nobody called instrument())
```

The subprocess inherits `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` from the parent's environment, but:

1. **Nobody calls `OpenAIInstrumentor().instrument()`** in the subprocess — its LLM calls produce no OTel spans
2. **Nobody iterates its event stream** — its `ToolExecutionStarted`/`Completed` events are never seen by Langfuse code
3. **Nobody passes `LANGFUSE_PARENT_OBSERVATION_ID`** — even if the subprocess created traces, they'd be orphaned top-level traces, not children of the parent span

---

## The Fundamental Difference

```
Claude: One process, one stream, everything visible
═══════════════════════════════════════════════════

Our process
└── client.receive_response()         ← SINGLE async iterator
    ├── main agent messages           ← parent_tool_use_id = null
    ├── sub-agent messages            ← parent_tool_use_id = "toolu_..."
    ├── sub-agent tool results        ← parent_tool_use_id = "toolu_..."
    ├── task lifecycle messages        ← TaskStarted, TaskNotification
    └── final result                  ← ResultMessage with cost

    We see EVERYTHING → we trace EVERYTHING


OpenHarness: Two processes, two streams, only parent visible
═══════════════════════════════════════════════════════════

Our process                          Subprocess
└── engine.submit_message()          └── engine.submit_message()
    ├── AssistantTurnComplete            ├── AssistantTurnComplete
    ├── ToolStart("agent")               ├── ToolStart("read_file")
    ├── ToolDone("Spawned...")           ├── ToolDone(file contents)
    ├── AssistantTextDelta               └── AssistantTurnComplete
    └── AssistantTurnComplete
                                         ↑ INVISIBLE to us
    We see THIS ↑ only
```

The Claude SDK chose **multiplexed observability** — sub-agent work streams through the parent. OpenHarness chose **process isolation** — sub-agents run independently with their own event loops.

This is an architectural trade-off, not a bug:

| | Claude SDK | OpenHarness |
|---|---|---|
| **Optimized for** | Observability — everything in one stream | Parallelism — independent processes |
| **Sub-agent visibility** | Full — every LLM call and tool use visible | None — only spawn confirmation and final exit code |
| **Process model** | Single process, async multiplexing | Multi-process, subprocess isolation |
| **Tracing depth** | Deep nesting (parent → agent → sub-agent tool) | Flat (parent → agent spawn/complete) |
| **What fixing it requires** | Nothing — works by design | Modifying the openharness package to instrument subprocesses |

---

## What Can Be Captured Today

### Claude Provider

| Data | OTel Path | Langfuse Path |
|------|-----------|---------------|
| LLM calls (model, tokens) | Yes — native SDK spans | Yes — from AssistantMessage |
| Tool calls (input, output) | No — SDK doesn't emit tool spans | Yes — from ToolUseBlock/UserMessage |
| Sub-agent LLM calls | Yes — SDK emits them in same trace | Yes — from AssistantMessage with parent_tool_use_id |
| Sub-agent tool calls | No | Yes — from ToolUseBlock with parent_tool_use_id |
| Agent lifecycle (start/stop) | No | Yes — from TaskStarted/TaskNotification |
| Cost | No | Yes — from ResultMessage.total_cost_usd |
| Per-model breakdown | No | Yes — from ResultMessage.model_usage |
| Prompt/response text | Redacted by default | Full text captured |

### OpenHarness Provider

| Data | OTel Path | Langfuse Path |
|------|-----------|---------------|
| LLM calls (model, tokens) | Yes — OpenLLMetry intercept | Yes — from AssistantTurnComplete |
| Tool calls (input, output) | No — outside API call | Yes — from ToolExecutionStarted/Completed |
| Sub-agent LLM calls | **No** — subprocess not instrumented | **No** — subprocess event stream not iterable |
| Sub-agent tool calls | **No** | **No** |
| Agent start/stop | No | Partial — start from ToolExecutionStarted, stop possible via completion listener (not yet wired) |
| Cost | No — Ollama is free | No |
| Token counts | Zero — Ollama streaming limitation | Zero — same limitation |
| Prompt/response text | Full (OpenLLMetry captures messages) | Full (from event stream) |

### Closing the gap

| Enhancement | Effort | What it adds |
|-------------|--------|-------------|
| Wire `BackgroundTaskManager.register_completion_listener()` | Low | Agent span gets real end time, status, output |
| Pass `OTEL_*` env vars to subprocess + call `OpenAIInstrumentor()` | Medium | Sub-agent LLM calls appear in Tempo (separate trace) |
| Pass `LANGFUSE_PARENT_OBSERVATION_ID` to subprocess | Medium | Sub-agent observations nest under parent (requires openharness package change) |
| OpenHarness adds native telemetry to `run_task_worker()` | Package change | Full sub-agent visibility matching Claude |
