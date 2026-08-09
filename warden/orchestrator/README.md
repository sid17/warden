# orchestrator/ — L2: the engine (per-turn lifecycle)

> **Layer / role:** L2 — the orchestrator: the per-turn lifecycle and the composition root.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §6 (sessions & the per-turn lifecycle), §7a (the 3-stage permission chain), §5 L2, §12 verbs.
> **Inbound:** driven by a drive path — `drive/` (in-process `ChatAPI`/CLI) and, over HTTP, `harness_api/`.
> **Outbound:** `providers/` (L1), `workspace/` (L0), and the L3 services `safety/` · `persistence/` · `observability/`; the `seams/` protocols; `schemas/` contracts.

**"Orchestrator" here means only L2** — the engine that runs one turn (not the whole
runtime). A **run** is one session's execution; a **turn** is one send-and-response within it.

## What's here

| Path | What it is |
|---|---|
| `orchestrator.py` | `Orchestrator.send_message` — the per-turn lifecycle and composition root; runs the permission chain (`ToolScope → PermissionChecker → PermissionHandler`) inline via `_can_use_tool`. |
| `stream_runtime.py` | Turn setup: session resolution, image prep, persisted-turn glue (stateless helpers extracted from the lifecycle). |
| `session/` | Session manager · index · SQLite DB — session lifecycle + resume across processes. |

## The turn (see [§6](../docs/01-conceptual-model.md#s6))

```
send(prompt)
  → input middleware (seam)         ── transform | reject
  → provider.send → asyncio.Queue → output middleware (seam) → events out
        └ tool_use → _can_use_tool (scope → checker → handler)   ← permission seam
  → snapshot workspace (persistence, keyed by (user_id, task_id))
```

## How it connects

- A session binds to **one** `(workspace, workflow)`; its policy is built **once at init**
  and never re-derived. A `send` naming a different workflow is **rejected**
  ([§6](../docs/01-conceptual-model.md#s6)).
- The permission chain runs for **every** provider: in-process providers call
  `_can_use_tool` directly; subprocess providers reach it via the permission bridge
  ([§9](../docs/01-conceptual-model.md#s9)).
