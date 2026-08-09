# Audit Hook Architecture: How Audit Logging Works

> How the harness captures tool calls, agent lifecycle, and session events as JSONL audit logs for both Claude and OpenHarness providers — using each provider's native hook system but producing identical output.

## Audit vs Telemetry

Audit and telemetry are independent systems solving different problems:

| | Telemetry (OTel + Langfuse) | Audit (JSONL hooks) |
|---|---|---|
| **Purpose** | Real-time observability, cost tracking, session analytics | Post-hoc compliance review, permission tuning, safety analysis |
| **Output** | Tempo traces, Prometheus metrics, Langfuse sessions | JSONL files on disk, one per run |
| **Activation** | Config-gated (`TelemetryConfig.enable_telemetry`) | Config-gated (`AuditConfig.enabled`) |
| **Retention** | Backend-managed (Tempo, Langfuse DB) | Local files, user-managed |
| **Consumers** | Grafana dashboards, Langfuse UI | `aggregate.py` reports, `scan_output.py` checks, human review |

The two systems share no code, no data, and no dependencies. They can run simultaneously or independently.

---

## Architecture Overview

```
                          ┌─────────────────────────────────┐
                          │    Provider Session (send())     │
                          └──────────┬──────────────────────┘
                                     │
                    ┌────────────────┼─────────────────┐
                    ▼                                   ▼
          Claude SDK Path                    OpenHarness Path
                    │                                   │
                    ▼                                   ▼
    ClaudeAgentOptions.hooks            HookRegistry + HookExecutor
    (in-process async callbacks)        (command hooks → subprocess)
                    │                                   │
                    ▼                                   ▼
    audit_hook() in                    python -m warden.observability
    observability/audit/               .audit.openharness_hook_handler
    claude_sdk_hooks.py                 (runs in child process per event)
    (runs in parent process)
                    │                                   │
                    └───────────────┬───────────────────┘
                                    ▼
                        AuditEvent (schemas/audit.py)
                                    │
                                    ▼
                          AuditLogWriter.append()
                                    │
                                    ▼
                        audit/logs/{run_id}.jsonl
```

Both paths converge on the same `AuditEvent` dataclass and the same JSONL format. The aggregation script, reports, and compliance tools work identically across providers.

---

## Claude SDK: In-Process Callbacks

### How hooks are registered

The Claude Agent SDK exposes an async callback hook system. We register a single callback (`audit_hook`) for all event types via `ClaudeAgentOptions.hooks`:

```python
# In observability/audit/claude_sdk_hooks.py
def build_audit_hooks() -> dict[str, list[HookMatcher]]:
    event_types = [
        "PreToolUse", "PostToolUse", "PostToolUseFailure",
        "SubagentStart", "SubagentStop", "Stop", "Notification",
    ]
    return {
        et: [HookMatcher(matcher=None, hooks=[audit_hook], timeout=5.0)]
        for et in event_types
    }

# In ClaudeSession — merged into options.hooks when AuditConfig.enabled
options.hooks = build_audit_hooks(run_id=..., log_dir=...)
```

### How events flow

```
Claude SDK agent loop
│
├── About to call tool "Write"
│   └── fires PreToolUse hook
│       └── audit_hook(hook_input, tool_use_id, context)   ← in-process, async
│           ├── Reads hook_input["hook_event_name"] = "PreToolUse"
│           ├── Reads hook_input["tool_name"], ["tool_input"], etc.
│           ├── Constructs AuditEvent(event_type="PreToolUse", ...)
│           ├── AuditLogWriter.append(event) → audit/logs/{run_id}.jsonl
│           └── Returns {} (never blocks)
│
├── Tool executes...
│
├── Tool finished
│   └── fires PostToolUse hook
│       └── audit_hook(...) → same flow → JSONL line appended
│
├── Sub-agent spawned → SubagentStart hook → JSONL
├── Sub-agent exited  → SubagentStop hook  → JSONL
├── Model stops       → Stop hook          → JSONL
└── Permission prompt  → Notification hook  → JSONL
```

### Key properties

- **In-process**: The callback runs in the same Python process as the agent. No subprocess spawn, no IPC. Fast.
- **Async**: The callback is `async def`. It awaits nothing (just file I/O), so it's effectively synchronous.
- **Never blocks**: Always returns `{}`. Errors are caught and logged, never propagated.
- **Full visibility**: The SDK fires hooks for both parent and sub-agent events (multiplexed onto the same stream), so audit captures everything.

---

## OpenHarness: Command Hook Subprocess

### How hooks are registered

OpenHarness has a native hook system with `HookRegistry` → `HookExecutor`. We use the programmatic API to register `command` type hooks:

```python
# In observability/audit/openharness_hooks.py
def build_openharness_audit_hooks(cwd, api_client, model) -> HookExecutor:
    registry = HookRegistry()
    for event in [PRE_TOOL_USE, POST_TOOL_USE, SUBAGENT_STOP, STOP, NOTIFICATION]:
        registry.register(event, CommandHookDefinition(
            command=f"{sys.executable} -m warden.observability.audit.openharness_hook_handler",
            matcher=None,
            block_on_failure=False,
        ))
    context = HookExecutionContext(cwd=cwd, api_client=api_client, default_model=model)
    return HookExecutor(registry, context)

# In providers/openharness/hook_setup.py — built when AuditConfig.enabled,
# then passed to QueryEngine(..., hook_executor=hook_executor)
hook_executor = build_openharness_audit_hooks(...)
```

### How events flow

```
OpenHarness QueryEngine agent loop
│
├── About to call tool "run_shell_command"
│   └── executor.execute(PRE_TOOL_USE, payload)
│       └── _run_command_hook(hook, event, payload)
│           ├── Serializes payload as JSON
│           ├── Sets env: OPENHARNESS_HOOK_PAYLOAD=<json>, OPENHARNESS_HOOK_EVENT=pre_tool_use
│           ├── Inherits parent env: AUDIT_RUN_ID, AUDIT_LOG_DIR, PYTHONPATH, etc.
│           ├── Spawns: python -m warden.observability.audit.openharness_hook_handler
│           │   └── Handler subprocess:
│           │       ├── Reads $OPENHARNESS_HOOK_PAYLOAD
│           │       ├── json.loads() the payload
│           │       ├── Maps "pre_tool_use" → AuditEvent(event_type="PreToolUse", ...)
│           │       ├── Maps tool names: "write_file" → "Write" for summarization
│           │       ├── AuditLogWriter.append(event) → audit/logs/{run_id}.jsonl
│           │       └── Exits 0
│           └── HookResult(success=True, blocked=False)
│
├── Tool executes...
│
├── Tool finished → POST_TOOL_USE hook → subprocess → JSONL
├── Sub-agent exited → SUBAGENT_STOP hook → subprocess → JSONL
├── Model stops → STOP hook → subprocess → JSONL
└── Permission prompt → NOTIFICATION hook → subprocess → JSONL
```

### Key properties

- **Out-of-process**: Each hook event spawns a new Python subprocess. Slower than in-process, but fully isolated.
- **Environment-based payload**: The `HookExecutor` sets `$OPENHARNESS_HOOK_PAYLOAD` (JSON) and `$OPENHARNESS_HOOK_EVENT` (string) as env vars. The handler reads these.
- **Environment inheritance**: The subprocess inherits `**os.environ` from the parent — so `AUDIT_RUN_ID`, `AUDIT_LOG_DIR`, and `PYTHONPATH` carry through automatically.
- **Never blocks**: `block_on_failure=False` on all hooks. Handler exits 0 always. Errors logged, never propagated.
- **`sys.executable` for Python path**: macOS doesn't have `python` on PATH (only `python3`), so we resolve the correct interpreter at registration time.

---

## Event Type Mapping

Both providers produce the same `event_type` values in JSONL. The mapping from each provider's native event names:

| JSONL `event_type` | Claude SDK hook name | OpenHarness hook event | When it fires |
|--------------------|--------------------|----------------------|---------------|
| `PreToolUse` | `PreToolUse` | `pre_tool_use` | Before tool execution |
| `PostToolUse` | `PostToolUse` | `post_tool_use` (is_error=false) | After successful tool execution |
| `PostToolUseFailure` | `PostToolUseFailure` | `post_tool_use` (is_error=true) | After failed tool execution |
| `SubagentStart` | `SubagentStart` | — | Sub-agent spawned (Claude only) |
| `SubagentStop` | `SubagentStop` | `subagent_stop` | Sub-agent subprocess exited |
| `Stop` | `Stop` | `stop` | Model ends its turn |
| `Notification` | `Notification` | `notification` | Permission prompt or info |

**Note:** OpenHarness has no `SubagentStart` event — the `subagent_stop` hook fires when the subprocess exits, but there's no corresponding start hook. Claude SDK has both.

---

## Tool Name Mapping

OpenHarness uses different tool names than Claude SDK. The audit handler normalizes them for input summarization (content stripping on write/edit tools):

| OpenHarness tool | Claude SDK equivalent | Summarization behavior |
|-----------------|----------------------|----------------------|
| `write_file` | `Write` | Drops `content` field from `tool_input_summary` |
| `edit_file` | `Edit` | Drops `old_string`, `new_string` fields |
| `run_shell_command` | `Bash` | Truncates `command` at 200 chars |
| Others | (pass-through) | Default: truncate long values at 100 chars |

The actual `tool_name` in the JSONL event retains the original OpenHarness name. The mapping only affects which summarization strategy is applied to `tool_input_summary`.

---

## JSONL Output Format

Both providers produce identical JSONL structure with OTel-aligned field names:

```json
{
  "event_type": "PreToolUse",
  "timestamp": "2026-06-22T10:30:00.123456+00:00",
  "run_id": "my-run-1",
  "session_id": "sess-abc",
  "tool_name": "run_shell_command",
  "tool_input_summary": {"command": "echo hello"},
  "gen_ai.operation.name": "execute_tool",
  "gen_ai.tool.name": "run_shell_command"
}
```

Key format rules:
- **OTel dot-notation**: Fields prefixed `gen_ai_` in the dataclass serialize as `gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.tool.name`
- **None fields excluded**: Missing fields are omitted, not null
- **Compact JSON**: No whitespace, `separators=(",", ":")`
- **One line per event**: Standard JSONL — `\n`-delimited

---

## The Fundamental Difference

```
Claude SDK: In-process, synchronous, full visibility
══════════════════════════════════════════════════

Our process
└── SDK agent loop
    ├── PreToolUse   → audit_hook() → JSONL       ← callback runs HERE
    ├── tool runs...
    ├── PostToolUse  → audit_hook() → JSONL       ← callback runs HERE
    ├── sub-agent starts → SubagentStart hook      ← sub-agent events
    ├── sub-agent tools  → Pre/PostToolUse hooks     are MULTIPLEXED
    ├── sub-agent stops  → SubagentStop hook         onto same stream
    └── Stop         → audit_hook() → JSONL

    One process. One callback. Every event visible.


OpenHarness: Out-of-process, subprocess per event, parent-only visibility
═════════════════════════════════════════════════════════════════════════

Parent process                      Handler subprocess (per event)
└── QueryEngine agent loop          └── python -m …observability.audit.openharness_hook_handler
    ├── PRE_TOOL_USE ──spawn──→         ├── reads $OPENHARNESS_HOOK_PAYLOAD
    │                                   ├── maps to AuditEvent
    │                                   ├── appends to JSONL
    │                                   └── exits 0
    ├── tool runs...
    ├── POST_TOOL_USE ──spawn──→        (same flow)
    ├── SUBAGENT_STOP ──spawn──→        (fires when subprocess exits)
    └── STOP ──spawn──→                 (same flow)

    Sub-agent internals (its tool calls, LLM calls) are NOT audited.
    Only the parent's view: spawn and exit.
```

This mirrors the telemetry gap described in `telemetry-architecture.md`:
- Claude SDK multiplexes sub-agent events → audit sees everything
- OpenHarness isolates sub-agents as subprocesses → audit sees spawn/exit only

---

## Activation and Configuration

Audit is **config-gated**, not env-gated. It activates when the threaded
`AuditConfig.enabled` is true — `if self._audit and self._audit.enabled`. The
config carries `run_id` and `log_dir`; the old `AUDIT_ENABLED` env var survives
only as a settings alias for that config field (`audit_enabled` in
`config/settings.py`, mapped onto `AuditConfig.enabled`).

### Config fields (`AuditConfig`)

| Field | Default | Purpose |
|----------|---------|---------|
| `enabled` | `False` | Master switch — gates hook installation for both providers |
| `run_id` | `run-default` | Names the JSONL file: `audit/logs/{run_id}.jsonl` |
| `log_dir` | `audit/logs/` | JSONL output directory |

**OpenHarness note:** the OH hooks are command *subprocesses* that read
`AUDIT_RUN_ID` / `AUDIT_LOG_DIR` from the environment at fire time. So the setup
code *derives* those env vars from `AuditConfig` at the subprocess boundary
(config → env), rather than reading them as input. `PYTHONPATH` still matters for
module resolution in the handler subprocess.

### Wiring in session code

**Claude** (`providers/claude/session.py`): the `AuditConfig` slice is threaded
into the session; when enabled, the audit matchers are merged into
`options.hooks`:
```python
if self._audit and self._audit.enabled:
    from warden.observability.audit.claude_sdk_hooks import build_audit_hooks
    self._merge_hooks(options, build_audit_hooks(
        run_id=self._audit.run_id,
        log_dir=Path(self._audit.log_dir) if self._audit.log_dir else None,
    ))
```

**OpenHarness** (`providers/openharness/hook_setup.py`): same gate, plus the
config→env derivation described above:
```python
if audit is not None and audit.enabled:
    os.environ["AUDIT_RUN_ID"] = audit.run_id
    if audit.log_dir:
        os.environ["AUDIT_LOG_DIR"] = str(audit.log_dir)
    from warden.observability.audit.openharness_hooks import build_openharness_audit_hooks
    audit_executor = build_openharness_audit_hooks(...)
engine = QueryEngine(..., hook_executor=audit_executor)
```

---

## File Map

| File | Role |
|------|------|
| `schemas/audit.py` | `AuditEvent` dataclass — shared by both providers |
| `observability/audit/claude_sdk_hooks.py` | Claude SDK: `audit_hook()` callback + `AuditLogWriter` + `build_audit_hooks()` |
| `observability/audit/openharness_hooks.py` | OpenHarness: `build_openharness_audit_hooks()` — registry + executor constructor |
| `observability/audit/openharness_hook_handler.py` | OpenHarness: CLI entry point — reads env var payload, maps to AuditEvent, writes JSONL |
| `providers/openharness/hook_setup.py` | OpenHarness: config-gated hook-executor builder (permission gate + audit) |
| `observability/audit/aggregate.py` | Report generator — reads all JSONL files, produces tool usage matrix, path access map |
| `observability/audit/scan_output.py` | Output scanner — detects leaked system prompts, internal paths in generated files |
| `observability/audit/logs/` | JSONL output directory — one file per `run_id` |
| `tests/observability/audit/test_hooks.py` | Claude SDK audit unit tests |
| `tests/observability/audit/test_openharness_hooks.py` | OpenHarness audit unit tests |
| `tests/observability/audit/test_smoke.py` | Claude SDK smoke test validation |
| `tests/observability/audit/test_openharness_smoke.py` | OpenHarness smoke tests (needs Ollama) |

---

## Design Decisions

| Decision | Why |
|----------|-----|
| **Command hooks (not HTTP or prompt)** | Simplest hook type. No server to run, no model invocation. Just a subprocess that writes a file. |
| **`sys.executable` in command string** | macOS has no `python` binary — only `python3`. Using the running interpreter's path guarantees the subprocess uses the same Python. |
| **Tool name mapping for summarization** | OpenHarness tools (`write_file`, `edit_file`) need the same content-stripping as Claude SDK tools (`Write`, `Edit`). Mapping happens in the handler, not in `AuditEvent`. |
| **Same `AuditEvent` schema, no new fields** | OpenHarness payloads map cleanly to existing fields. No schema divergence means aggregation works unchanged. |
| **`block_on_failure=False` on all hooks** | Audit must never block the agent pipeline. If the handler crashes, the agent continues. |
| **Programmatic registration (not settings.json)** | We already construct `QueryEngine` (via `hook_setup.py`). Building the registry in code is simpler than managing a settings file, and activation is controlled by the threaded `AuditConfig.enabled` (not a settings file). |
| **No `SubagentStart` for OpenHarness** | OpenHarness has no `subagent_start` hook event. The `subagent_stop` event fires natively when the subprocess exits — this is sufficient for audit (we know what ran and how it ended). |
