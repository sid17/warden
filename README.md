# Warden — a tenant-isolated, provider-agnostic agent runtime

> *Every agent runs under a Warden.* Given `(user_id, task_id)`, a prompt, and a provider,
> Warden runs an AI agent in an **isolated workspace** under a **fixed permission policy** and
> **streams typed output** — many runs at once, keyed by `(user_id, task_id)`. You bring the
> agent and its policy; the runtime handles isolation, human-in-the-loop approvals, resource
> governance, streaming, resumable persistence, and observability.
>
> **The name.** *Warden* — the runtime every agent runs under: isolated, gated, governed.
> The Python package is `warden` (`import warden`). "The harness" appears in the docs as a
> synonym for the runtime.

---

## The problem

Building an agent demo is easy; **productionizing** one is the hard part, and everyone
re-solves the same plumbing. To run an agent for real you need **isolation** (one user's run
can't touch another's files or credentials), **control** (gate which tools it may call; pause
for a human on the risky ones), **portability** (not welded to one vendor's SDK), **cost/time
bounds**, **resumability** (a long run survives a crash), and **observability**. Done per-app,
that glue usually **couples the safety/isolation logic to the product** — so none of it is
reusable or verifiable.

The bet: all of that is *mechanism* — it belongs in a runtime, once. What's left for the app
is *policy*: what the prompt says, which tools are allowed, who may spend. Draw that line
cleanly and any workflow can deploy onto one runtime.

---

## What it is

**One contract.** The developer brings three things; the runtime owns the rest.

| You define | The runtime handles |
|---|---|
| **Agent** — the workspace: skills + sub-agents + which provider runs them | isolation by `(user, task)` · provider portability · streaming · snapshot/resume |
| **Workflow** — the named permission surface a run binds to | session lifecycle · the permission chain · fail-closed deny-baseline |
| **Policy** — permissions that travel *with* the workflow (file access · tool allow/deny · mode) + middleware + custom tools + cost/time caps | enforcement + HITL approvals · the resource governor · observability (OTel + tracing + audit) |

**Governing principle — mechanism vs. policy.** The runtime owns the **verbs** (run, stream,
isolate, enforce, persist, resume); the app owns the **nouns** (what prompt, which decision,
which filter, who pays). Policy reaches the engine only through a few **seams** — permissions ·
middleware · custom tools · resource governance — so the core stays product-agnostic and never
bakes a decision in. (`warden` imports no product code: `grep` proves it.)

**Provider-agnostic.** Claude SDK, Codex, and a local OpenHarness/Ollama provider sit behind
one `AgentProvider` contract, chosen per run by how much isolation/control that run needs.

---

## Install

Warden uses [uv](https://docs.astral.sh/uv/) (Python 3.11–3.14):

```bash
git clone https://github.com/sid17/warden.git
cd warden
uv sync                              # core; add --extra postgres / --extra telemetry as needed
uv run pytest -q                     # hermetic suite — no creds or network required
cp .env.example .env                 # then fill in provider creds to drive a real agent
```

## How to use it

There are two ways to drive the engine and two ways your product integrates. Full guide:
[`docs/product_integration.md`](warden/docs/product_integration.md).

### 1. In-process (`ChatAPI`) — same process, raw events

```python
from warden.drive.api import ChatAPI

# config carries the tenant identity (config.workspace.user_id / .task_id) and provider;
# see docs/05 + docs/09 for building it.
api = ChatAPI(config, repo_path=workspace_dir, workflow="my-workflow")
await api.init()
async for event in api.send("...your prompt..."):
    ...  # typed events: session, token, tool_use, checkpoint, result/error
await api.close()
```

See [`docs/05-app-interaction-patterns.md`](warden/docs/05-app-interaction-patterns.md).

### 2. As an HTTP service (the Runs API) — any language, multi-tenant, durable

```bash
# product-agnostic server (no profile):
python -m uvicorn warden.harness_api.profiles.serve:app --host 0.0.0.0 --port 8080
```

```
POST /runs        {user_id, task_id, session_id?, input, sink, budget_usd?}  → 202 {run_id}
GET  /runs/{id}/events   (SSE)   |   sink:{type:"webhook",url}   (push)
POST /runs/{id}/tool_confirmation  {decision}      # resume a human-gated run
```

Full contract (routes, egress, auth, seeds, state backends):
[`docs/12-the-runs-api.md`](warden/docs/12-the-runs-api.md).

### 3. Teach it your product — a profile

To have the agent call **your** tools (writing results back to your DB) and run **your**
skills, ship a **profile** — a `list[CustomTool]` + a seeded workspace — in your own package,
and point `WARDEN_PROFILE` at its fully-qualified module path. The engine loads it out-of-tree;
your product code never enters the engine. Playbook:
[`docs/10-adding-a-profile.md`](warden/docs/10-adding-a-profile.md).

### The two integration shapes

| | **Long-horizon (job)** | **Streaming (chat)** |
|---|---|---|
| Agent runs for | minutes; multi-agent, calls your tools, may pause at a human gate | seconds; interactive turns |
| You consume | checkpoints, tool events, final `result` (webhook sink) | live token stream (SSE / in-process) |
| Writes back to your DB? | yes, via custom tools | usually no |
| Needs a profile? | yes | usually no |

See [`docs/product_integration.md`](warden/docs/product_integration.md) for when to use each and the
five-step spine.

---

## The tree, by layer

Each folder is a layer or a seam of the model in
[`docs/01-conceptual-model.md`](warden/docs/01-conceptual-model.md).

| Folder | Layer | What it is |
|---|---|---|
| [`workspace/`](warden/workspace/) | **L0** | what the agent operates on — task folder, workflow manifest, skills/agents scaffolding |
| [`providers/`](warden/providers/) | **L1** | the `AgentProvider` adapters (claude · codex · openharness) + auth |
| [`orchestrator/`](warden/orchestrator/) | **L2** | the engine: per-turn lifecycle, sessions, the permission chain |
| [`safety/`](warden/safety/) · [`persistence/`](warden/persistence/) · [`observability/`](warden/observability/) | **L3** | cross-cutting services that wrap every run |
| [`drive/`](warden/drive/) | **L4a** | in-process drive paths — `ChatAPI`, CLI |
| [`harness_api/`](warden/harness_api/) | **L4b** | the HTTP Runs API + the account/billing (keys/governance) wrapper, kept distinct from the engine |
| [`harness_api_mock/`](warden/harness_api_mock/) | — | a wire-faithful mock harness (canned scripts) for $0 deterministic integration testing |
| [`seams/`](warden/seams/) | **§7** | the policy-injection protocols — permissions · middleware · custom tools |
| [`schemas/`](warden/schemas/) | **§3** | the typed contracts — events · providers · tool_scope · audit |

Supporting: [`config/`](warden/config/) (one typed config surface: env layer → nested `HarnessConfig`
→ builder), `__init__.py` (public API), `__main__.py` (`python -m warden`), `docs/`,
`tests/`, `docker/`.

---

## Documentation

Start at [`docs/README.md`](warden/docs/README.md) — the numbered chapters, read in order. The most
useful entry points:

- **[`docs/01-conceptual-model.md`](warden/docs/01-conceptual-model.md)** — what the harness *is*: layers, seams, mechanism vs. policy.
- **[`docs/product_integration.md`](warden/docs/product_integration.md)** — how to drive it from a product (the two shapes).
- **[`docs/10-adding-a-profile.md`](warden/docs/10-adding-a-profile.md)** — the profile seam (custom tools + seed).
- **[`docs/12-the-runs-api.md`](warden/docs/12-the-runs-api.md)** — the HTTP contract.
- **[`docs/11-permissions-and-human-in-the-loop.md`](warden/docs/11-permissions-and-human-in-the-loop.md)** — the permission chain + durable HITL.
- **[`docs/06-resource-governance.md`](warden/docs/06-resource-governance.md)** · **[`docs/02-observability.md`](warden/docs/02-observability.md)** · **[`docs/03-safety.md`](warden/docs/03-safety.md)** · **[`docs/04-audit.md`](warden/docs/04-audit.md)** — the cross-cutting services.
- **[`docs/09-environment-and-credentials.md`](warden/docs/09-environment-and-credentials.md)** — auth & credentials, secrets-by-reference.
- Provider notes: [`docs/providers/`](warden/docs/providers/) · task runbooks: [`docs/guides/`](warden/docs/guides/).

---

## Status

The runtime contract, the seams, the provider abstraction, the permission chain + durable
HITL, two-axis tenancy (tenant-isolated but account-agnostic), and OTel/tracing/audit
observability are all built and exercised end-to-end — including a live product integration
(a course-generation pipeline: workspace + skills → landscape → approval gate → research →
write → index) driven through a real UI, backend, and DB. Single-node today; the multi-replica
Postgres state backend is implemented (`WARDEN_STATE_BACKEND=postgres`) — see
[`docs/12-the-runs-api.md` §10](warden/docs/12-the-runs-api.md).
