# Product Integration — connecting the harness to your product

> **What this is.** The one-page map for a *product* team adopting the harness. It answers
> "how do I drive this engine from my app?" — the two integration **shapes** the harness
> supports, when to use each, and the exact seams/steps for both. It is the entry point;
> each step links to the deep doc that specifies it.
>
> **The mental model in one line.** The harness is a **tenant-isolated execution engine**;
> your product is the **control plane** that dispatches work to it and consumes its typed
> event stream. The engine names no product — you teach it yours through **one seam** (a
> *profile*), and everything else (auth, streaming, governance, telemetry, HITL, isolation)
> is generic mechanism you configure, not code you fork.
>
> Companions: [`01-conceptual-model.md`](./01-conceptual-model.md) (what the harness *is*),
> [`10-adding-a-profile.md`](./10-adding-a-profile.md) (the profile seam — the how-to for
> the tool/writeback half), [`12-the-runs-api.md`](./12-the-runs-api.md) (the HTTP contract),
> [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) (the in-process path),
> [`11-permissions-and-human-in-the-loop.md`](./11-permissions-and-human-in-the-loop.md) (HITL).

---

## 1. The boundary — who owns what

```
   YOUR PRODUCT (control plane)                 THE HARNESS (this engine)
 ┌───────────────────────────────┐           ┌──────────────────────────────────────┐
 │ frontend · backend · your DB  │           │  POST /runs {spec, sink} ──▶ run_id    │
 │ job queue / session bookkeeping│──dispatch─▶│  isolated per-run agent (a provider)  │
 │ a PROFILE: your tools + seed   │◀──events──│  auth · governance · telemetry · HITL │
 │ an events consumer (relay/tap) │◀─writeback│  typed Event stream → your chosen sink│
 └───────────────────────────────┘           └──────────────────────────────────────┘
```

- **Dependency direction is one-way: product → harness, never the reverse.** The engine
  imports no product code. The *only* place product-specific coupling is allowed is a
  **profile** (see [`10`](./10-adding-a-profile.md)), which lives in **your** package and is
  loaded by a fully-qualified `WARDEN_PROFILE` — so the engine ships open-source while your
  profile stays in your repo. (This is why `grep -r 'your_product' warden` returns
  nothing.)
- You never handle model API keys, workspace isolation, or concurrency — the engine resolves
  the credential server-side ([`09`](./09-environment-and-credentials.md)), isolates each
  `(user, task)`, and bounds its own concurrency.
- You **do** own: dispatching runs, consuming events, resolving HITL gates, and (if your flow
  writes results) the tools that write back to your DB.

---

## 2. Two integration shapes

Both drive the **same** engine over the **same** Runs API; they differ in the *interaction
horizon* and how you consume output. Most products use one; some use both (e.g. a long
authoring job **and** an interactive chat).

### Shape A — Long-horizon agentic run with tool calls (the "job" shape)

A multi-step agent runs for **minutes**, calls **your** in-process tools to write results
back to your DB, may **pause at a human gate**, and produces durable artifacts. You watch
progress through checkpoint/tool events; the outcome is persisted work, not a live transcript.

- **Use when:** course/report/document generation, research, codebase migration, data
  enrichment — anything where the agent does substantial work and the *result* matters more
  than the token-by-token stream.
- **You provide:** a **profile** — (1) a `list[CustomTool]` (your in-process MCP tools that
  write back), and (2) a **seeded workspace** (your skill/agent/doc files). See
  [`10-adding-a-profile.md`](./10-adding-a-profile.md).
- **How it runs:** `POST /runs` with a **`webhook` sink** → the agent runs, calling your
  tools (each is a real writeback to your DB) → checkpoint/tool events POST to your webhook →
  a confirm-required tool **pauses** the run (`requires_action`) → you resolve it with
  `POST /runs/{id}/tool_confirmation` → the run completes with a terminal `result`.
- **Durability:** webhook egress + `GET /runs/{id}/history?after=<seq>` replay; survives
  restarts and multi-replica ([`12` §6, §10](./12-the-runs-api.md)).
- **Reference shape:** a course-creation integration (create → landscape gate → research →
  write → a `complete` tool) is the canonical example of this shape; the profile that
  implements it lives in the product's own integration package, not in this engine.

### Shape B — Streaming real-time (the "chat" shape)

Dispatch a run and stream **tokens/events live** to a user as they arrive — low-latency,
interactive, transcript-oriented. The value is the live stream, not a durable artifact.

- **Use when:** an interactive assistant / chat, a live "explain this" panel, a dev-chat —
  the user watches output appear and reacts.
- **You provide:** typically **no profile** (the model answers directly), or a light tool set
  if the chat needs actions. No seed if it's pure Q&A.
- **How it runs:** either
  - **In-process** — import `ChatAPI` and async-iterate its events directly (same process, no
    network hop): [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md); or
  - **As a service** — `POST /runs` with an **`sse` sink**, hold open
    `GET /runs/{id}/events`, and **relay** each `token`/`checkpoint` event to your UI
    (commonly SSE→WebSocket in your backend).
- **Caveat:** SSE is **single-replica / sticky-session** — the buffer is pinned to the replica
  running the run. For multi-replica live UIs, relay through your backend, or use
  webhooks + history-polling ([`12` §6](./12-the-runs-api.md)).
- **Reference:** a product-side SSE→WebSocket relay that consumes the Runs-API SSE and forwards it
  into a browser WebSocket (one harness, many consumers).

### Choosing

| | **A — Long-horizon (job)** | **B — Streaming (chat)** |
|---|---|---|
| Horizon | minutes; multi-turn, multi-agent | seconds; interactive turns |
| What you consume | checkpoints, tool events, final `result` | live `token`/`checkpoint` stream |
| Sink ([`12` §6](./12-the-runs-api.md)) | **webhook** (durable push) | **sse** (live pull) or in-process `ChatAPI` |
| Writes back to your DB? | yes — via your **custom tools** ([`10`](./10-adding-a-profile.md)) | usually no (transcript only) |
| Human-in-the-loop gate? | common (`requires_action` → `/tool_confirmation`) | rare |
| Needs a profile? | **yes** (tools + seed) | usually **no** |
| Durability | high (webhook + history replay, multi-replica) | ephemeral / single-replica |
| Reference | a course-creation pipeline | dev-chat SSE→WS relay |

---

## 3. The integration spine (five steps)

Both shapes share this spine; Shape A adds step 3, Shape B usually skips it.

1. **Run the harness.** In-process (`from warden.drive.api import ChatAPI`,
   [`05`](./05-app-interaction-patterns.md)) or as a service (the profile-aware entrypoint
   `warden.harness_api.profiles.serve:app`, [`12`](./12-the-runs-api.md)). Credentials
   and env: [`09`](./09-environment-and-credentials.md).
2. **Authenticate the caller.** Set `SERVICE_TOKENS_JSON` and send `x-service-token` +
   `x-user-id`; an empty registry runs open (single-tenant dev). Per-run owner isolation is
   automatic ([`12` §7](./12-the-runs-api.md)).
3. **Teach it your product — the profile (Shape A).** Ship a `list[CustomTool]` + a seed
   bundle, exposed as `PROFILE` in your package, and point `WARDEN_PROFILE` at its
   fully-qualified module path. Full playbook: [`10-adding-a-profile.md`](./10-adding-a-profile.md).
4. **Dispatch + consume.** `POST /runs` with your `RunSpec` (`user_id`, `task_id`,
   `session_id?`, `input`, `sink`, optional `budget_usd`/`deadline`/`max_turns`), then consume
   the typed `Event` stream via your sink ([`12` §5–6](./12-the-runs-api.md)). Costs/limits are
   the Governor's ([`06`](./06-resource-governance.md)).
5. **Handle HITL (if you gate).** On `requires_action`, surface the `permission_request` to a
   human and resume with `POST /runs/{id}/tool_confirmation` (`approve` / `reject` /
   `revise+feedback`). Mechanism: [`11`](./11-permissions-and-human-in-the-loop.md).

---

## 4. Where your code lives (the open-source boundary)

- **The engine** (`warden/`) is self-contained and open-source-safe: zero imports of
  any product package. You depend on it; it never depends on you.
- **Your profile** lives in **your** package (e.g. `yourco_integration/<product>/real` for the
  live server and `.../mock` for the mock harness) and is loaded out-of-tree by a
  fully-qualified `WARDEN_PROFILE` / `MOCK_WARDEN_PROFILE`. A bare name resolves against the
  engine's built-in `profiles/` package (where the shipped **`example`** profile lives); a
  dotted name is imported verbatim — that dotted form is the seam that keeps your product code
  out of the engine.
- **Your product data** (skill/agent/doc content, the seed manifest) stays in your product
  repo; the profile *reads and validates* it, it does not vendor a copy.

The invariant to hold: *if `grep` for your product noun hits anything under `warden/`,
that's a leak.* Keep it in your profile package.

---

## 5. See also

- [`10-adding-a-profile.md`](./10-adding-a-profile.md) — the profile seam (custom tools + seed
  + bring-up), the how-to for Shape A.
- [`12-the-runs-api.md`](./12-the-runs-api.md) — the full HTTP contract: routes, RunSpec,
  egress (webhook/SSE), seeds/provisioning, auth, state backends.
- [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) — the in-process
  `ChatAPI` path (Shape B, no network hop).
- [`11-permissions-and-human-in-the-loop.md`](./11-permissions-and-human-in-the-loop.md) — the
  durable HITL mechanism behind gates.
- [`06-resource-governance.md`](./06-resource-governance.md) — budgets, deadlines, the Governor.
