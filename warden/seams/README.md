# seams/ — the three seams: how policy plugs in

> **Layer / role:** cross-cutting protocols — the *only* way application policy enters the engine.
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §7 (the seams), §2 (mechanism vs policy), §15.
> **Inbound:** the orchestrator (L2) calls these at each turn boundary; `safety/` implements the middleware seam; `providers/` deliver custom tools.
> **Outbound:** `schemas/` types only — these are leaf protocol definitions.

The harness owns the **verbs** (run, stream, enforce, persist); the application owns the
**nouns** (what prompt, which decision, which filter). Nouns reach the engine through
**exactly three seams**, and this folder holds their **protocols**. Their *enforcement /
implementation* lives in the layer that owns it — the folder is the contract, the layer is
the mechanism.

## What's here

| File | Seam | The app supplies… | Enforced/implemented in |
|---|---|---|---|
| `permissions.py` | Permission handler (§7d) | how a confirmation reaches a human (`AutoAllowHandler` / `CLIPermissionHandler` / WebSocket / durable-HTTP) | the chain in `orchestrator/` + the checker in `safety/permissions/` |
| `middleware.py` | Middleware (§7b) | input/output interception — `Middleware` / `SendContext` / `RejectResult` (transform or reject) | `safety/middleware/` |
| `custom_tools.py` | Custom tools (§7c) | tools the agent may call — `CustomTool` (list, or an MCP server) | `providers/` (in-process list, or MCP for all providers) |

## How it connects

- A seam is a **boundary**: the engine calls out to it and never bakes the decision in
  ([§2](../docs/01-conceptual-model.md#s2)). This is what keeps the harness a mechanism.
- The permission **handler** here is only *how* a confirmation reaches a human — it holds
  **no rules**; the rules come from the workflow manifest and are evaluated by the checker
  ([§7a](../docs/01-conceptual-model.md#s7a)).
