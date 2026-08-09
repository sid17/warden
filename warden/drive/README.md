# drive/ — L4a: the in-process drive paths

> **Layer / role:** L4a — the engine's own in-process surfaces (pure mechanism, no policy).
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §11 (the two drive paths), §12 (verbs), §5 L4.
> **Inbound:** a Python app that `import`s the engine, or the interactive CLI; `harness_api/` wraps `ChatAPI` for the HTTP path.
> **Outbound:** `orchestrator/` (L2), the `seams/` protocols (permission handlers), `schemas/` (events, tool scope).

The **in-process** ways to drive the engine. This is the engine's own surface — it carries
**no** accounts/keys/budgets policy. The HTTP drive path and the Axis-2 policy that comes
with it live separately in `harness_api/` ([§8](../docs/01-conceptual-model.md#s8)); the
mechanism/policy line is why these are two folders, not one.

## What's here

| File | What it is |
|---|---|
| `api.py` | `ChatAPI` — Python-native `init` / `send` / `resume` / `close`; wraps the `Orchestrator`. |
| `cli.py` | The interactive terminal driver (`python -m warden`). |

## How it connects

- **Human-in-the-loop is synchronous here** ([§11](../docs/01-conceptual-model.md#s11)):
  a `PermissionHandler` from `seams/` blocks the turn until the human answers — ideal for
  tight interactive loops. (Over HTTP the same approval becomes *durable* — that's
  `harness_api/`.)
- Multi-tenancy on this path is **the app's own** — you run your own `ChatAPI` instances
  and pass your own `auth_env`; there is no managed-key registry or spend cap here.
