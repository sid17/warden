# providers/ — L1: the provider adapters

> **Layer / role:** L1 — run an AI system; adapters are capability *profiles*, so one vendor may have several.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §5 L1, §9 (uniting isolation & control), §10 (auth resolves at this boundary).
> **Inbound:** the orchestrator (L2) creates a session and drives `send()`.
> **Outbound:** `schemas/providers.py` (the `AgentProvider` protocol), `providers/auth.py` (per-run auth), the `seams/` permission chain (in-process directly; subprocess via a permission bridge).

Each provider implements the `AgentProvider` protocol (`start`, `send`, `stop`, `close`) and
adapts a specific LLM backend into the orchestrator's unified event stream. Providers are
**capability profiles**, not one-per-vendor: `claude` (SDK, in-process) vs `claude-cli`
(subprocess), and `codex` (`codex exec`, auth-only) vs the planned `codex-mcp` (full control
via `mcp-server` approvals) — see [§9](../docs/01-conceptual-model.md#s9). Auth resolves here
via [`auth.py`](auth.py) ([§10](../docs/01-conceptual-model.md#s10)).

## Provider Protocol

Defined in `schemas/providers.py`. Every provider exposes the same interface:

```python
class AgentProvider(Protocol):
    session_id: str          # Captured from first streaming message (not pre-assigned)
    jsonl_path: str | None   # Path to transcript file (for history replay)

    async def start() -> None          # Initialize SDK/subprocess, connect
    async def send(prompt: str) -> AsyncGenerator[Any, None]  # Stream raw events
    async def stop() -> None           # Abort current stream
    async def close() -> None          # Tear down session
```

## Session Creation — What Goes In

The orchestrator calls `create_session()` with these parameters:

| Parameter | Type | What it controls |
|-----------|------|-----------------|
| `repo_path` | `Path` | Working directory for the agent — all file ops scoped here |
| `provider` | `str` | `"claude"`, `"codex"`, or `"openharness"` — selects the session class |
| `model` | `str \| None` | Model name (e.g., `"claude-sonnet-4-20250514"`, `"qwen3:1.7b"`) |
| `can_use_tool` | `callable` | Permission callback — orchestrator's `PermissionChecker` wrapped as async function |
| `resume_session_id` | `str \| None` | Resume an existing session (Claude only — SDK supports it natively) |
| `disallowed_tools` | `list[str]` | Tools the agent cannot use — derived from `ToolScope.to_disallowed_tools()` |
| `system_prompt` | `str \| None` | Override the default system prompt (safety experiments) |

### Permission Flow — `can_use_tool`

The orchestrator passes a `can_use_tool` callback that each provider calls before executing a tool:

```
Agent wants to use "Write" tool
  → Provider calls can_use_tool("Write", {"file_path": "..."})
  → Callback routes to PermissionChecker
    → Check workflow YAML rules (allowed_tools, denied_tools)
    → Check sensitive paths (.env, credentials)
    → Check file access globs (read/write patterns)
  → Returns True (allow) or False (deny)
  → If deny: provider skips tool, returns denial message to model
```

This is how the safety layer reaches into providers without providers knowing about workflows, permissions, or tool scopes. The provider just calls a function and gets a boolean.

### How each provider handles permissions

| Provider | Mechanism | Notes |
|----------|-----------|-------|
| **Claude** | `ClaudeAgentOptions.can_use_tool` | SDK calls this before every tool execution. Blocking async. |
| **OpenHarness** | `PermissionChecker` integration in `QueryEngine` | OpenHarness has its own permission system; we replace it with our callback. |
| **Codex** | `openai_codex` SDK `approval_handler` | The `codex` provider is the SDK adapter (`sdk_session.py`). We inject a fail-closed approval handler on the sync `CodexClient` and start the thread under `approval_policy=untrusted` (NOT `auto_review`, which auto-approves before our handler). Exec + file-change escalations map to `can_use_tool("Bash"/"Edit", ...)`; `decline` blocks the side effect. `perm_tier=arg_level`. The retired `codex exec` adapter (`session.py`, gated under `codex-exec`) had no gating. |

## Message Output — What Comes Out

Each provider's `send()` yields raw SDK/framework events. The **message handler** transforms these into normalized `MessageEvent` dicts that the orchestrator wraps as `MessageEvent(kind=..., content=...)`.

### Message Kinds

Every provider maps its native events to the same set of message kinds:

| Kind | What it represents | Content fields |
|------|-------------------|----------------|
| `stream_delta` | Partial text chunk (streaming) | `text` |
| `stream_end` | End of a streaming text block | — |
| `text` | Complete text block | `text` |
| `thinking` | Model's reasoning (extended thinking) | `text` |
| `tool_use` | Model wants to call a tool | `toolName`, `toolCallId`, `toolInput` |
| `tool_result` | Tool execution result | `toolCallId`, `toolResult`, `isError` |
| `status` | Metadata (session created, token usage, completion) | `subtype`, varies |
| `error` | Error during execution | `text` |

### Provider Event Mapping

How each provider's native events map to the unified kinds:

| Native Event | Provider | → Message Kind |
|-------------|----------|---------------|
| `StreamEvent` (content_block_delta, text_delta) | Claude | `stream_delta` |
| `StreamEvent` (content_block_stop) | Claude | `stream_end` |
| `AssistantMessage` with `TextBlock` | Claude | `text` |
| `AssistantMessage` with `ThinkingBlock` | Claude | `thinking` |
| `AssistantMessage` with `ToolUseBlock` | Claude | `tool_use` |
| `AssistantMessage` with `ToolResultBlock` | Claude | `tool_result` |
| `ResultMessage` | Claude | `status` (subtype: result, includes token usage) |
| `AssistantTextDelta` | OpenHarness | `stream_delta` |
| `AssistantTurnComplete` | OpenHarness | `status` (subtype: result) |
| `ToolExecutionStarted` | OpenHarness | `tool_use` |
| `ToolExecutionCompleted` | OpenHarness | `tool_result` |
| `ErrorEvent` | OpenHarness | `error` |
| JSON `type: "message"` | Codex | `text` |
| JSON `type: "tool_call"` | Codex | `tool_use` |
| JSON `type: "tool_result"` | Codex | `tool_result` |

### Common Content Shape

All message handler output follows this shape (consumed by the WebSocket transport):

```json
{
  "kind": "text",
  "id": "uuid",
  "sessionId": "session-uuid",
  "timestamp": 1719600000.0,
  "text": "The response content"
}
```

```json
{
  "kind": "tool_use",
  "id": "uuid",
  "sessionId": "session-uuid",
  "timestamp": 1719600000.0,
  "toolName": "Write",
  "toolCallId": "toolu_abc123",
  "toolInput": {"file_path": "/tmp/test.py", "content": "print('hello')"}
}
```

## Session Lifecycle

```
create_session(provider="claude", repo_path=..., can_use_tool=..., ...)
  │
  ▼
session.start()          # Initialize SDK/subprocess, set up hooks
  │
  ▼
session.send(prompt)     # Returns AsyncGenerator of raw events
  │                        → message_handler transforms each → MessageEvent
  │                        → First message: session_id captured
  │                        → Session registered in DB
  ▼
session.send(prompt)     # Subsequent messages reuse same session
  │
  ▼
session.stop()           # Abort current stream (user cancelled)
  │
  ▼
session.close()          # Tear down — kill subprocess, flush state
```

Session ID is **not pre-assigned** — it's captured from the provider's first response (`ResultMessage.session_id` for Claude, generated for OpenHarness). This ensures the orchestrator uses the provider's canonical session identity.

## Provider Capability Matrix

| Capability | Claude | OpenHarness | Codex |
|-----------|--------|-------------|-------|
| Integration | Library (SDK) | Library (Python) | Subprocess (CLI) |
| Streaming | Native | Native | JSON lines |
| Tool execution | SDK-managed | Framework-managed | CLI-managed |
| Permission hook | `can_use_tool` callback | `PermissionChecker` | Not supported |
| System prompt | `ClaudeAgentOptions.system_prompt` | Constructor param | Not supported |
| Custom tools | MCP registration | Tool registry (before `start()`) | Not supported |
| Session resume | `options.resume = session_id` | Not supported | Not supported |
| Disallowed tools | `ClaudeAgentOptions.disallowed_tools` | Excluded from registry | Not supported |
| Audit hooks | `ClaudeAgentOptions.hooks` (in-process) | `HookExecutor` (subprocess) | Not supported |
| OTel telemetry | Native (env vars) | OpenLLMetry instrumentor | Not supported |
| Langfuse tracing | `ClaudeLangfuseTracer` | `OpenHarnessLangfuseTracer` | Not supported |
| Thinking/reasoning | `ThinkingBlock` → `thinking` kind | Parse `<think>` tags | Not supported |
| Sub-agents | Native (Agent tool) | Fire-and-forget | Not supported |
| JSONL transcript | `~/.claude/projects/**/{id}.jsonl` | `~/.openharness/sessions/{id}.jsonl` | `~/.codex/sessions/**/*.jsonl` |

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | `create_session()` factory — routes to the right session class |
| `claude/session.py` | `ClaudeSession` — wraps `claude-agent-sdk` |
| `claude/message_handler.py` | `transform_sdk_message()` — converts Claude SDK events to normalized dicts |
| `codex/sdk_session.py` | `CodexSdkSession` — **the `codex` provider** — wraps the `openai-codex` Python SDK (fail-closed exec/patch gating) |
| `codex/sdk_message_handler.py` | `approval_to_tool_call()` + `notification_to_event()` — SDK approval/notification mapping |
| `codex/session.py` | `CodexSession` — **RETIRED** `codex exec` subprocess adapter (kept mv-only; gated at the factory under `codex-exec` → `NotImplementedError`) |
| `codex/message_handler.py` | `transform_codex_message()` — retired exec-JSONL → dict mapping (used by the retired adapter) |
| `openharness/session.py` | `OpenHarnessSession` — wraps OpenHarness `QueryEngine` |
| `openharness/message_handler.py` | `transform_openharness_message()` — converts OpenHarness events to normalized dicts |
