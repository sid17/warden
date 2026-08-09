# harness_api/ — L4b: the HTTP Runs API + Axis-2 policy wrapper

> **Layer / role:** L4b — the HTTP drive path **plus** the account/billing policy wrapper. **Kept distinct from the engine.**
> **Implements:** [`01-conceptual-model`](../docs/01-conceptual-model.md) §8 (Axis-2 tenancy), §10 (KeyRegistry + the Governor), §11 (the HTTP path), §12 (Runs API verbs).
> **Inbound:** external systems over HTTP (`POST /runs`, SSE/webhook).
> **Outbound:** `drive/` (`ChatAPI`) and the engine; `harness_api/credentials/` for key/budget resolution.

This is the **wrapper**, not the engine. The engine is tenant-*isolated* but
account-*agnostic*: it never sees a key registry or a dollar. This layer adds the *policy*
the engine deliberately excludes — *which* key a user runs on, *what* budget they have,
*whether* they can afford a run — and exposes it over HTTP. That mechanism/policy line is
why this is a top-level sibling of `drive/`, not nested inside it.

## What's here

| Path | What it is |
|---|---|
| `app.py` | The Runs API: `POST /runs`, `GET /runs/{id}/events`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `POST /runs/{id}/tool_confirmation`. |
| `runner.py` | Concurrency (`Semaphore(N)`), per-task locks, the spend gate, key selection, event adaptation. |
| `egress.py` | SSE / webhook delivery. |
| `credentials/keys.py` | `KeyRegistry` — per-user key/budget map → resolves a per-run `auth_env` (config names `secret_env`, never a secret). |
| `governance/ledger.py` | The `Governor`'s reservation ledger — per-`(user,task)` cost accounting + the **pre-run** budget gate (reserves worst-case up front, settles to actual after). |

## How it connects

- **HITL is durable here** ([§11](../docs/01-conceptual-model.md#s11)): a run *pauses* at a
  tool call, releases its slot, and resumes via `POST /runs/{id}/tool_confirmation` keyed by
  `(run_id, tool_use_id)` — approval survives the request/response boundary.
- No secrets cross the wire: the harness resolves the user's managed key from the registry
  by `user_id` ([§10](../docs/01-conceptual-model.md#s10)). The engine only ever receives one
  resolved credential for one run.
