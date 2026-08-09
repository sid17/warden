<a id="s0"></a>
# 12 — The Runs API

> **What this is.** The harness can be driven two ways: **in-process** through
> [`ChatAPI`](./05-app-interaction-patterns.md) (import it, call it, get events back in
> the same process), or **as an HTTP service** through the **Runs API** (`harness_api/`).
> This is the contract for the second: the HTTP surface an external product implements to
> adopt the harness *as a service* — the run lifecycle, the routes, how events and files
> leave the run (egress), the two-layer auth, and the `local`-vs-`postgres` state backends
> that turn one container into a fleet. Companions:
> [`01-conceptual-model.md`](./01-conceptual-model.md) (the seams this API wraps),
> [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) (the *in-process*
> path this one mirrors), [`09-environment-and-credentials.md`](./09-environment-and-credentials.md)
> (the credential model behind the service token), and
> [`11-permissions-and-human-in-the-loop.md`](./11-permissions-and-human-in-the-loop.md)
> (the durable HITL mechanism this doc only sketches lives there).
> Written, like its companions, as settled design.

---

<a id="s1"></a>
## 1. The idea in one sentence

**The Runs API is the HTTP contract that runs the harness as a service: `POST` a
`RunSpec`, get back a `run_id` and a stream of typed, `seq`-stamped `Event`s — the same
run the in-process `ChatAPI` gives you, but behind a network boundary, multi-tenant, and
durable across a restart.**

In-process, an app holds a `ChatAPI` object and iterates its events directly (`05`). The
Runs API is that same engine wrapped in a thin FastAPI app (`harness_api/app.py`) over a
concurrent `Runner`: the app owns *nothing* — it validates, delegates to the `Runner`,
and shapes the response; the `Runner` spawns one background `asyncio` task per run, each
building its own `ChatAPI`. The wire schemas (`harness_api/schemas.py`) *are* the contract.

---

<a id="s2"></a>
## 2. In-process vs. service — when to use which

Both drive the identical engine; they differ only in the boundary. Pick by where the
caller lives and what durability it needs.

| | **In-process (`ChatAPI`)** | **Service (Runs API)** |
|---|---|---|
| Entry point | `from warden.drive.api import ChatAPI` | `POST /runs` over HTTP |
| Where the caller runs | Same Python process as the harness | Any language, any host — a product backend |
| Events | Async-iterate `OrchestratorEvent`s directly | Typed `Event`s via **egress** (webhook push / SSE pull) |
| Multi-tenant | Caller owns isolation | Service token + per-run owner check built in |
| Concurrency | Caller's problem | `Runner` bounds it (per-`(user,task)` lock + `Semaphore(N)`) |
| Durability | Ephemeral to the process | Run identity + event log survive a restart |
| Credentials | Caller passes `auth_env` | Resolved server-side from the managed-key registry (`09`) |
| Reference | [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) | this doc |

**Rule of thumb.** If the caller is Python *inside* the harness deployment and wants raw
events with no network hop, use `ChatAPI` directly (`05`'s in-process example). If the
caller is a separate product — a different service, a different language, needing
tenancy, durability, and a stable HTTP surface — use the Runs API. One harness, many
consumers; see [`product_integration.md`](./product_integration.md) for the two integration
shapes a product builds on this API.

---

<a id="s3"></a>
## 3. The run lifecycle — the state machine

A run's status is the `RunStatus` literal in `schemas.py`. It is a wire fact returned by
`GET /runs/{id}` (`RunView.status`); internally the `Runner` sets it as the run advances
(`_runner_exec.py`). The **terminal** event on the stream is always exactly one
`result` or `error` (`Event.type`); `stopped` is the terminal for a deliberate Governor
halt. A `requires_action`/`paused` run is **not** terminal — it holds, awaiting a
decision, and resumes into the *same* egress sink.

```
                         POST /runs
                             │
                             ▼
                        ┌─────────┐
                        │ queued  │   registered; task spawned, awaiting a slot
                        └────┬────┘   (per-(user,task) lock + Semaphore(N))
                             ▼
                        ┌─────────┐
                        │ running │   ChatAPI turn(s) streaming events
                        └────┬────┘
          ┌──────────────────┼───────────────────┬───────────────┐
          ▼                  ▼                    ▼               ▼
   ┌───────────────┐   ┌──────────┐        ┌───────────┐   ┌───────────┐
   │requires_action│   │  paused  │        │  stopped  │   │ cancelled │
   │(HITL: awaiting │   │(Governor │        │(Governor  │   │(caller    │
   │ tool_confirm)  │   │ hold /   │        │ halt:     │   │ POST      │
   └───────┬───────┘   │ top-up)  │        │ budget /  │   │ /cancel)  │
           │           └────┬─────┘        │ deadline /│   └───────────┘
           │ POST           │ resume       │ max_turns)│    terminal:error/…
           │ /tool_         │              └───────────┘
           │ confirmation   │               terminal: stopped
           ▼                ▼
        ┌─────────┐   (back to running)
        │ running │
        └────┬────┘
             ▼
   ┌───────────────┐        ┌───────────┐
   │  succeeded    │        │   error   │
   │ terminal:     │        │ terminal: │
   │ Event result  │        │ Event error│
   └───────────────┘        └───────────┘
```

| Status | Meaning |
|---|---|
| `queued` | registered at submit; the background task is spawned but not yet past the lock/semaphore. `RunState` default. |
| `running` | a `ChatAPI` turn is streaming events. |
| `requires_action` | a durable HITL gate fired — the run paused at a confirm-required tool, awaiting `POST /runs/{id}/tool_confirmation`. **Not terminal**; the worker slot is already released, so a paused run pins nothing. |
| `paused` | a Governor/HITL hold (e.g. a pausable budget top-up path). **Not terminal.** |
| `stopped` | a deliberate Governor halt — budget, deadline, or `max_turns`. Terminal, and distinct from `error` (a failure) and `cancelled` (a caller abort). |
| `cancelled` | a caller aborted via `POST /runs/{id}/cancel`. Terminal. |
| `succeeded` | the run finished cleanly; the terminal `result` event carries `{result, usage, cost_usd}`. |
| `error` | the run failed; the terminal `error` event carries `{reason}`. |

> The wire event vocabulary (`EventType`) is broader than the status set — it includes
> `session` (always first, carries the resolved `session_id`), `checkpoint`, `token`,
> `tool_use`/`tool_result`, `compaction`, the HITL `permission_request`/`permission_resolved`/
> `permission_expired`, and the `completion`/`milestone` re-tag targets. Every event
> carries a monotonic per-run `seq`.

---

<a id="s4"></a>
## 4. The routes — the full contract

Enumerated directly from `harness_api/app.py`. **Auth column:** `token` = the app-wide
`require_service_token` dependency (every route except `/health`); `+owner` = the
run-scoped `require_run_owner` dependency layered on top (see [§7](#s7)).

| Method · Path | Body | Returns | Auth |
|---|---|---|---|
| `GET /health` | — | `{"status":"ok"}` · 200 | **none** (liveness probe; exempt) |
| `POST /runs` | `RunSpec` | `RunAccepted` · **202** | `token` (establishes the owner) |
| `POST /seeds` | raw `.tar.gz` body | `SeedAccepted` · **201** | `token` (seeds are product-global) |
| `POST /provision` | `ProvisionSpec` | `ProvisionAck` · 200 | `token` (+ `x-user-id` must match `spec.user_id` when configured) |
| `GET /runs/{run_id}` | — | `RunView` · 200 (404 unknown) | `token` `+owner` |
| `GET /runs/{run_id}/file?path=…` | — | file bytes (`application/octet-stream`) · 200 (400 bad path, 404 absent) | `token` `+owner` |
| `GET /runs/{run_id}/events` | — | SSE `text/event-stream` (404 no buffer) | `token` `+owner` |
| `GET /runs/{run_id}/history?after=<seq>` | — | `list[Event]` (durable replay) · 200 | `token` `+owner` |
| `POST /runs/{run_id}/cancel` | — | `{run_id, status:"cancelling"}` · 200 (404 not cancellable) | `token` `+owner` |
| `POST /runs/{run_id}/tool_confirmation` | `ToolConfirmation` | recorded decision · 200 (404 unknown, 422 empty revise) | `token` `+owner` |

> The module docstring names only "four routes" (the original L1 core); the shipped app
> has ten. `POST /provision`, `POST /seeds`, `GET /runs/{id}/file`, and
> `GET /runs/{id}/history` were added later (EXT-W1 / EXT-A1 / C2) and are live in `app.py`.

---

<a id="s5"></a>
## 5. Submitting a run — `POST /runs`

The body is a `RunSpec` (`schemas.py`). The required fields identify the tenant and the
work; the rest are optional bounds and per-run choices:

| Field | Meaning |
|---|---|
| `user_id`, `task_id` | the tenant + the unit of serialization. Runs on one `task_id` **serialize** (per-task lock); concurrency is *across* tasks. |
| `session_id` | omit ⇒ a new session; supply ⇒ **resume** it (creation and revision of a course share one `session_id`). |
| `provider`, `model` | which adapter/model runs it (`provider` defaults to `claude`). |
| `input` | opaque, skill-specific prompt/params (e.g. `input.workflow` binds init-time session identity). |
| `sink` | a `Sink` — `{type: "webhook", url, headers}` or `{type: "sse"}`. Chooses egress ([§6](#s6)). |
| `budget_usd`, `deadline`, `max_turns` | optional per-run bounds the Governor/HITL layer enforces (`06`). All `None` ⇒ unbounded. |

`POST /runs` validates (a `webhook` sink without a `url` → **422**), calls
`Runner.submit(spec)`, and returns **202** with `RunAccepted{run_id}` — a UUID hex,
unique across restarts. It does **not** block on the run: `submit` registers a
`_RunState`, spawns one background `asyncio` task, and returns immediately. Under load the
run **queues** internally (the per-`(user,task)` `asyncio.Lock` then a global
`Semaphore(N)`); there is **no `429`** — the harness bounds its own concurrency, so a busy
`task_id` serializes rather than being rejected.

`POST /runs` is the one run-touching route gated by `token` **only**, not `+owner`: it
*establishes* the owner, so there is no prior owner to check against.

---

<a id="s6"></a>
## 6. Getting output — events, files, and egress

A run produces a **stream of `Event`s** and, when a skill writes them, **files**. Three
read surfaces, one egress seam.

**The event stream.** Every event is typed, carries a monotonic per-run `seq`, starts
with a `session` event and ends with exactly one `result`/`error` (`schemas.py`). Two ways
to consume it live, chosen per run by the `RunSpec.sink` and realized by one of two
**egress adapters** (`egress.py`, behind the `EgressAdapter` seam):

- **`WebhookEgress` (push).** `sink.type == "webhook"`: each event is `POST`ed as JSON to
  `sink.url`. The product persists on receive, so it is naturally durable; a delivery
  failure is retried a few times, then **logged and counted** (`delivery_failures`) —
  never silently swallowed — but it does not abort the run (the product owns redelivery).
  Best for checkpoints and cross-service delivery.
- **`SseEgress` (pull).** `sink.type == "sse"`: events are buffered for a
  `GET /runs/{id}/events` hold-open (`text/event-stream`). The buffer means a
  late-joining consumer still sees the stream from its first event; the stream closes
  after the terminal event. **Single-replica only** — the buffer is pinned to the replica
  that *runs* the run, so a load balancer that routes the `GET` elsewhere 404s. Best for
  token streams into a sticky-session / single-container UI.

**Durable replay (any replica).** `GET /runs/{id}/history?after=<seq>` reads the
append-only `run_events` log and returns every event with `seq > after` in order. Unlike
SSE this is *replica-agnostic* and survives teardown: a reconnecting consumer resumes from
its last-seen `seq` with no loss or duplication (the log is `INSERT-OR-IGNORE` on
`(run_id, seq)`). For a multi-replica deployment, prefer **webhooks or history-polling**;
SSE is the single-container path.

**Status and files.** `GET /runs/{id}` returns a `RunView` snapshot
(`status`, `session_id`, `last_seq`, `usage`, `cost_usd`, `error`) — reconstructed from
the durable event log after a restart, so it survives a redeploy. `GET /runs/{id}/file?path=…`
returns the bytes of one file from the run's snapshot (confined path; `..`/escape → 400).

---

<a id="s7"></a>
## 7. Auth — service token (caller) and run owner (per-run)

Two layers, both harness-owned, both defined in `app.py` / `credentials/service_tokens.py`.
The credential that runs the *model* is a separate concern (`09`); this section is only
about *who may call the API*.

**Layer 1 — authentication (`require_service_token`, app-wide).** Every route except
`/health` requires a known `x-service-token` header. The `ServiceTokenRegistry` holds a
`service_name → token` map (loaded inline-wins from `SERVICE_TOKENS_JSON` / `_FILE`). This
proves *which backend is calling*.

**Layer 2 — authorization (`require_run_owner`, run-scoped).** On the run-scoped routes,
the run's owning `user_id` (from the in-memory registry, or the durable `runs` table after
a restart) must equal the caller-asserted `x-user-id`, else **403**; an unknown run → **404**
(distinguishing "no such run" from "wrong user"). This is the read-side enforcement of
tenant isolation — a valid token can never read another user's run.

**Single-tenant vs multi-tenant.** The two layers share one switch: an **empty** token
registry is **open** — every call is allowed and ownership is *not* enforced. That is the
intentional single-tenant dev default (one trusted caller, no tenancy to isolate). A
**non-empty** registry flips *both* layers on — `401` on a bad/missing token, `403` on a
cross-user access — giving the full multi-tenant isolation guarantee. `POST /provision`
carries the same correct-user guard (`x-user-id` must match `spec.user_id` when
configured); `task_id` is never gated — the caller is trusted to request the right task,
the only hard harness guarantee being correct-*user* isolation.

---

<a id="s8"></a>
## 8. HITL over the Runs API

When a run hits a confirm-required tool, it does not fail and it does not silently proceed:
it **pauses**. The run's status becomes `requires_action`, a `permission_request` event
rides the stream (carrying `{tool_use_id, tool_name, tool_input, reason}`), and — crucially
— the worker slot is *released*, so a paused run pins no concurrency. The product resumes it
with `POST /runs/{id}/tool_confirmation`, whose `ToolConfirmation` body carries a **decision**
(never a credential — the key is resolved server-side), keyed by `tool_use_id`, in three
modes: **`approve`** (run the tool, proceed), **`reject`** (refuse, halt, tell the model not
to retry), **`revise`** (refuse but re-plan with non-empty `feedback` — an empty `revise` is
**422**). The confirm is idempotent on `(run_id, tool_use_id)`; both the ask and its
resolution ride the `run_events` log, so they survive teardown and replay.

That is the *contract*. The **mechanism** — the durable-defer hook, the decision-aware
resume continuations, why this path is Claude-only, the SLA/expiry model — is covered in
[`11-permissions-and-human-in-the-loop.md`](./11-permissions-and-human-in-the-loop.md)
(§5–§8), with the runbook in
[`guides/permissions/durable-hitl-over-http.md`](./guides/permissions/durable-hitl-over-http.md).

---

<a id="s9"></a>
## 9. Seeds & provisioning

Before a run can use a product's skills/agents/workflows, that content must be *in* the
`(user, task)` workspace. Two routes, decoupled so a product uploads once and provisions
many:

- **`POST /seeds`** (`seeds.py`, `SeedStore`). The raw request body is a `.tar.gz` of
  `skills/`, `agents/`, `workflows/`. The response is `SeedAccepted{seed_ref}` — an
  **opaque, content-addressed, immutable** reference (`seed_` + sha256 of the bytes), so
  re-uploading identical bytes returns the same ref (dedupe) and a ref always resolves to
  the same content. Storage reuses the persistence `StorageBackend` (local or S3) — no new
  infra. An empty body → **422**. Not run-scoped (seeds are product-global) — gated by the
  token only.
- **`POST /provision`** (`ProvisionSpec{user_id, task_id, seed_ref}`). Lays the seed's
  skills/agents/workflows into the `(user, task)` task dir and returns `ProvisionAck`
  (`workflows`, `skills`, `copied`, `mkdirs`). **Idempotent** — bootstrap skips
  already-populated entries, so re-provision never clobbers produced work; an unknown
  `seed_ref` → **404**, a failed lay-down → **500** (nothing left partial). When caller-auth
  is configured, `x-user-id` must equal `spec.user_id` (provisioning writes into a user's
  box, so it carries the same correct-user guard as the run-scoped routes).

---

<a id="s10"></a>
## 10. State backends — `local` (default) vs `postgres`

One env switch — `WARDEN_STATE_BACKEND` (`StateBackendConfig.backend`, `config.py`) —
moves **every** durable store between its process-local backend and shared Postgres. It is
the single lever that converts a single container into a fleet member.

| Store | `local` backend (default) | `postgres` backend |
|---|---|---|
| Run registry (`run_registry.py`) | in-memory / append-only `runs.jsonl` | `PostgresRunRegistry` (shared `runs` table) |
| Event log (`event_log.py`) | aiosqlite `run_events.db` | `postgres_event_log.py` (shared) |
| Governance ledger (`06`) | in-memory / `balance.jsonl` | injected `PostgresReservationLedger` |
| Defer store (HITL) | filesystem defer store | shared Postgres |
| Task lock (`task_lock.py`) | `InProcessTaskLock` (`asyncio.Lock`) | `PostgresTaskLock` (shared `task_leases`) |

**`local`** — the lightweight JSONL / embedded-sqlite / filesystem stores: correct and
zero-dependency for a **single container** (the `asyncpg` driver is an optional extra, so
this path never imports it). **`postgres`** — the shared backend **required to run more
than one replica** behind a load balancer, because no store is process-local, so any
replica can serve, resume, or cold-resume any run (including a durable-HITL confirm that
lands on a different container than the one that paused).

**Multi-replica leasing & fencing (short version).** The cross-replica `(user, task)`
mutex is a **claim + lease** in the shared `task_leases` table (`PostgresTaskLock`):
acquisition is a conditional upsert that only takes a key whose existing lease is *expired*
by the **database** clock (never a replica's wall clock), a background heartbeat renews the
lease while the run executes, and renew/release are **owner-guarded** (they filter
`owner_id = me`, so a replica whose lease was legitimately stolen can neither resurrect nor
clobber the new owner — the fencing primitive). This ensures exactly one replica runs a
given `(user, task)` at a time. The full multi-replica model — leasing, fencing,
cold-resume, reconnection, and cross-replica live fan-out — is a scaling deep-dive of its
own; this section is only the contract-level summary the Runs API exposes.

---

<a id="s11"></a>
## 11. See also

- [`product_integration.md`](./product_integration.md) — how a product drives this API: the
  two integration shapes (long-horizon tool-calling jobs vs. real-time streaming) and the
  five-step spine.
- [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) — the in-process
  `ChatAPI` path this API mirrors.
- [`06-resource-governance.md`](./06-resource-governance.md) — the Governor behind
  `budget_usd`/`deadline`/`max_turns` and the `stopped` terminal.
- [`09-environment-and-credentials.md`](./09-environment-and-credentials.md) — the
  managed-key model behind the service token (which key runs the model).
- [`11-permissions-and-human-in-the-loop.md`](./11-permissions-and-human-in-the-loop.md)
  — the permission seam + the durable HITL mechanism behind `requires_action` →
  `/tool_confirmation` (runbook:
  [`guides/permissions/durable-hitl-over-http.md`](./guides/permissions/durable-hitl-over-http.md)).
