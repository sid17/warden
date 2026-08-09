# schemas/ — the data contracts (§3)

> **Layer / role:** cross-cutting — the typed I/O contracts every layer speaks.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §3 (the contract), §12 (verbs), §13.
> **Inbound:** every layer imports these types.
> **Outbound:** none (leaf — pure Pydantic/protocol definitions).

The typed data that crosses boundaries: events, the provider protocol, tool scope, and the
audit record. This is **only** data contracts now — two things that used to live here have
moved to where the model puts them:
- the **three seam protocols** (`permissions`, `middleware`, `custom_tools`) → [`../seams/`](../seams/) (§7).
- the **`Workflow` permission manifest** (loader + model) → [`../workspace/workflow/`](../workspace/workflow/) (L0, §4).

## What's here

| File | Contracts | Description |
|---|---|---|
| `events.py` | `OrchestratorEvent`, `SessionCreatedEvent`, `MessageEvent`, `CompletionEvent`, `ErrorEvent`, `ToolAccessNotificationEvent` | The typed event stream the orchestrator emits ([§3](../docs/01-conceptual-model.md#s3) output). |
| `providers.py` | `AgentProvider` | The L1 protocol every provider implements — `start()` · `send()` · `stop()` · `close()`. |
| `tool_scope.py` | `ToolScope` | Per-turn allow/deny by tool name — stage 1 of the enforcement chain ([§7a](../docs/01-conceptual-model.md#s7a)). |
| `audit.py` | `AuditEvent` | Per-tool-call audit record with OTel-aligned field names + JSONL serialization ([04-audit](../docs/04-audit.md)). |

## How it connects

- `AgentProvider` (here) is *implemented* by `providers/`; `ToolScope` (here) is *evaluated*
  by the orchestrator's permission chain against the workflow rules in `workspace/`.
- `AuditEvent` is emitted by `observability/audit/` and, aggregated, drives a workflow's
  permission config ([04-audit](../docs/04-audit.md)).
