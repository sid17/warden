<a id="s0"></a>
# The Harness — Design Concept Note

> The reference for what the harness engine (`warden/`) **is**: a tenant-isolated,
> provider-agnostic execution engine for running AI systems. It covers, in order:
>
> - the **principle** that decides what belongs in it (mechanism vs. policy);
> - its **layers**, its **workspace / workflow** model, its **sessions**, and its **seams**;
> - how it **isolates** and **authenticates** work, and how it unites **isolation with control**;
> - the **drive paths** and **verbs** an external system uses to run it.
>
> Companions:
> - [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) — how an app builds prompt-level
>   behavior on top of the harness.
> - [`03-safety.md`](./03-safety.md) — the safety model: the middleware pipeline, permission enforcement,
>   and how safety policy is *discovered* rather than guessed.
> - [`02-observability.md`](./02-observability.md) — the typed signals the harness emits and the backends
>   they feed.
> - [`04-audit.md`](./04-audit.md) — the durable record of what a run did, and how it drives a workflow's
>   permission config.
>
> This set is the harness's **final design** — the prod-ready architecture, stated as settled design.
> It is what the harness is meant to be; the implementation moves toward it.

---

<a id="s1"></a>
## 1. What the harness is (one sentence)

**A harness is a tenant-isolated execution engine: given a `(user_id, task_id)`, a prompt,
and a provider, it runs an AI system inside an isolated workspace and streams typed output —
and it can run many of these at once, keyed by `(user_id, task_id)`.**

That is the whole contract. Everything else is either a *mechanism* that serves that contract,
or a *policy* that belongs to whoever is driving the harness.

---

<a id="s2"></a>
## 2. The governing principle: mechanism vs. policy

Every decision about "does this belong in the harness?" is answered with one test:

- **Mechanism** — the machinery to *run* an AI system: isolate a workspace, invoke a provider,
  enforce a permission decision, stream events, persist, resume. **Mechanism belongs in the harness.**
- **Policy** — the decisions about *what* to run: what the prompt says, what a permission
  decision *is*, which safety filter to apply, who is allowed to spend.
  **Policy belongs to the application driving the harness.**

> The harness owns the **verbs** (run, stream, enforce, persist, resume, isolate).
> The application owns the **nouns** (what prompt, which decision, which filter).

Policy reaches the harness through a **seam** — a protocol/callback the app implements. The engine
calls out to the seam; it never bakes the decision in. The seams are the subject of [§7](#s7).

---

<a id="s3"></a>
## 3. The harness contract (input → output)

```
  INPUT ────────────────────────────────────────────────────────────
     user_id, task_id    ·    prompt (already framed)    ·    provider / model
     workspace   =  folder + skills + agents
     workflow    =  permissions  (file access + tool allow/deny + mode)   ← chosen from the workspace
     seams       =  permission handler · middleware · custom tools
                                     │
                                     ▼
  EXECUTION ────────────────────────────────────────────────────────
     1.  isolate the workspace by (user_id, task_id)
     2.  run the provider
     3.  enforce tool scope + permissions          ←  calls the seams
     4.  stream events
     5.  persist / restore the workspace
                                     │
                                     ▼
  OUTPUT ───────────────────────────────────────────────────────────
     event stream           =  session · token · tool_use · result · error · stopped
     persisted workspace    =  folder snapshot
     observability signals
```

The harness **never** decides what the prompt says or what a permission *decision* is. It only
runs what it is given and calls out to the seams for decisions.

---

<a id="s4"></a>
## 4. Workspace and workflow

A **workspace** is *what the agent operates on*. Concretely it is a task folder at
`base_dir/{user_id}/{task_id}/`, scaffolded with the **skills and agents** the agent may use
(`.claude/{skills,agents}`, copied from an explicit allowlist and recorded in `bootstrap.lock.json`
so the setup is reproducible). Think of it as the agent's sandbox: its files, plus its capabilities.

A **workflow** is a **permission manifest** *inside* that workspace — a `.workflows/*.yaml` parsed
into the `Workflow` model. It declares `name`, `description`, and `permissions`: which files the
agent may read/write, which tools it may call, and the permission mode. A workspace can hold
**several** workflows — each is a different *permission surface* over the same files and capabilities.

**Example.** A course-authoring workspace for user `u42`, task `course-async-python`:

```
  base_dir/u42/course-async-python/
  ├── .claude/
  │   ├── skills/{outline, draft, cite}      ← what the agent CAN do (shared across the workspace)
  │   └── agents/{researcher, editor}
  ├── .workflows/
  │   ├── research.yaml   → permissions: read notes/**, deny Write        (gather)
  │   ├── author.yaml     → permissions: write drafts/**, allow Bash      (produce)
  │   └── publish.yaml    → permissions: read drafts/**, write out/**     (finalize)
  └── drafts/  notes/  out/                    ← the agent's working files
```

Same folder and skills throughout; the *permission surface* changes with the workflow you pick.

Which workflow a run uses is chosen when its **session** starts — one session runs against exactly one
`(user_id, task_id, workflow)`. How that mapping works, and why changing workflow means a new session,
is [§6](#s6).

---

<a id="s5"></a>
## 5. The five layers

Numbered bottom-up: each layer depends only on the ones below it.

```
  L4    DRIVE PATHS               ChatAPI (in-process) · CLI · Runs API (HTTP)         drive/ · harness_api/
  L3    CROSS-CUTTING SERVICES    persistence · observability · audit · safety         persistence/ · observability/ · safety/
  L2    ORCHESTRATOR              per-turn lifecycle · permission chain · sessions      orchestrator/
  L1    PROVIDERS                 claude · codex · openharness  (AgentProvider)         providers/
  L0    WORKSPACE                 task folder · .workflows/*.yaml · .claude/{skills,agents}   workspace/
        ──────────────────────────────────────────────────────────────────────────────────────────
  ⊥     cross-cutting             the four seams (§7) · the data contracts (§3)         seams/ · schemas/
```

Each layer is a folder under `warden/`; the tree *is* this diagram. The two
cross-cutting boundaries — the four **seams** (`seams/`) and the **data contracts**
(`schemas/`) — thread every layer rather than sitting in one, so they stand outside the
bottom-up stack.

- **L0 Workspace** — [§4](#s4).
- **L1 Providers** — the `AgentProvider` protocol; `claude` (SDK), `claude-cli`/`codex`/`codex-mcp`
  (subprocess), `openharness` (local/Ollama) — providers are capability *profiles*, so one vendor may
  have several adapters ([§9](#s9)). Auth resolves at this boundary ([§10](#s10)); it is also where
  isolation and control are united ([§9](#s9)).
- **L2 Orchestrator** — the engine; the per-turn lifecycle and the permission chain ([§6](#s6),
  [§7a](#s7a)).
- **L3 Cross-cutting services** — persistence, observability, audit, safety; they wrap every run
  ([§13](#s13) inventory).
- **L4 Drive paths** — the interfaces/transports that drive the engine; two paths ([§11](#s11)) and
  their verbs ([§12](#s12)).

---

<a id="s6"></a>
## 6. Sessions and the per-turn lifecycle

A **run** is one session's execution; a **turn** is one send-and-response within it.

### A session binds to one (workspace, workflow)
A session is created for **one workspace + one workflow**, and its policy — the `PermissionChecker`
and the deny baseline — is built **once, at initialization**, and never re-derived mid-session. This
keeps the permission surface fixed and verifiable for the life of the session.

The `(user_id, task_id, workflow)` → session mapping is **directional**:

- one `(user_id, task_id, workflow)` → a session, uniquely identified by its `session_id`;
- one **workspace** (`user_id, task_id`) can host **many** sessions — one per workflow, or several
  over time;
- but a session belongs to **exactly one** `(user_id, task_id, workflow)` — **never the reverse.** A
  `session_id` is never re-pointed at a different workspace or workflow.

So to run a **different workflow**, the caller opens a **new session** deliberately. The workspace
folder is shared, so the new session restores the same folder from persistence and simply applies a
different permission manifest.

A `send` that names a workflow **different from the one the session is bound to is rejected with an
error** — the harness never silently re-points a live session at a new permission surface. Changing
the permission surface is an *explicit* new-session act, so the surface a `session_id` enforces is
fixed and verifiable for the session's whole life; the caller, not the engine, owns the decision to
switch.

### The lifecycle (`Orchestrator.send_message`)
The workspace is restored and the session established at initialization; each turn is then a clean
input → execution → output pass:

```
  send(prompt)
     │
     ▼
  input middleware ──▶ transform  |  reject → error                 ← seam (§7b)
     │
     ▼
  provider.send(prompt) ─▶ stream ─▶ queue ─▶ output middleware ─▶ events out
        │                                        (§7b seam)
        └── tool_use ─▶ _can_use_tool ─▶ allow | deny               ← seam (§7a)
                        (scope → checker → handler)
     │
     ▼
  snapshot workspace   (persistence: after the turn, keyed by (user_id, task_id))
```

**Data-flow notes.**

1. **The stream is decoupled from the consumer by an `asyncio.Queue`.** The provider produces events
   on its schedule and the caller consumes on its own; the queue between them buys three things:
   - **back-pressure both ways** — a slow consumer can't stall the provider mid-generation, and a slow
     provider can't monopolise the event loop;
   - **one ordered channel** — message events, permission notifications, and the terminal sentinel all
     arrive in the order they occurred;
   - **clean cancellation** — on abort the producer task is cancelled and the sentinel closes the turn
     with no half-delivered state.
2. The **`session_id`** is captured from the provider's first message and registered then.
3. Persistence is **restore-at-init / snapshot-after-turn**; a snapshot runs even if the turn errored
   (latest-overwrites; never lose partial work).
4. Tool calls are gated inline by `_can_use_tool` ([§7a](#s7a)) — the point at which a turn can pause
   for a human.

### Resume — one authoritative source per concern
"Resume" is really three stores, each authoritative for a different thing; they must not be conflated:

- **Workspace files** — the task folder, **restore-at-init / snapshot-after-turn** (local + S3, keyed
  by `(user_id, task_id)`). Authoritative for *what the agent's files look like*.
- **Session identity** — the session DB maps `(user_id, task_id, workflow)` → `session_id` + the
  provider's own resume token, so a new process re-attaches the *same conversation*.
- **Event history** — the durable append-only `run_events` log ([`02`](./02-observability.md),
  [`04`](./04-audit.md)): the record of everything that happened. It drives observability, audit, and
  durable-HITL replay ([§11](#s11)) — it is **not** re-executed to restore state. Resume replays
  *identity + files*; the event log explains *what occurred*.

### Compaction — long-running tasks
A long task overflows the context window; **compaction** summarizes the running transcript so the
session can continue. It fires at a **split point** — a turn boundary (safest), after a large
`tool_result`, at a token/context threshold, or a workflow checkpoint — and emits a `compaction` event
so it is observable. `claude` compacts **natively** (its auto-compact is the baseline); providers
without native compaction are driven by the harness (summarize, then resume with `summary + recent
turns`). The trigger policy travels **with the workflow** manifest — like safety and governance — so a
long-running task type carries its own compaction rules.

---

<a id="s7"></a>
## 7. The seams — how policy plugs in

Four seams surround the harness. Three gate the *content* of a turn (its messages, its tool calls);
the fourth bounds the *resources* a run may consume:

```
  HUMAN / WORLD            SEAMS                         HARNESS
  ─────────────            ─────                         ───────

  input   ─────────▶  [ input middleware ]  ─────────▶  ┐
                                                        │
  output  ◀────────  [ output middleware ]  ◀─────────  │  runs the task;
                                                        │  emits messages
  approved  ◀──────  [ permission check ]  ◀─────────   │  and tool calls
  tool call               (allow / deny)                │
                                ▲                       │
                    rules (workflow) + human (handler)  │
                                                        │
  continue /  ◀────  [ resource governor ]  ◀────────   ┘  reaches a checkpoint
  stop(reason)          (budget · deadline)                (turn end · tool gate · clock tick)
                                ▲
                    caps per (user, task) — Axis-2 policy
```

- **Regular messages** flow in and out through **middleware** (input and output — [§7b](#s7b)).
- **Tool calls** the agent emits are gated by the **permission** seam before they run ([§7a](#s7a)).
- **Custom tools** ([§7c](#s7c)) extend what the agent *can* call in the first place.
- **Resource governance** ([§7e](#s7e)) bounds a run's **cost and time** — at each checkpoint the
  engine asks the governor `continue` or `stop(reason)`, and obeys the verdict without seeing a dollar.

<a id="s7a"></a>
### 7a. Permissions — configuring and enforcing policy

**How policy is configured (levels):**
- **Harness level** — global defaults, e.g. the **deny-baseline** computed as the intersection of
  `tool_access.deny` across *all* workflows in the workspace (safe to apply at session creation).
- **Workflow level** — the specific workflow's permission manifest (file access + tool allow/deny +
  mode), chosen at session init ([§6](#s6)).

These are the only two *config* levels — where a rule **comes from**. A per-turn `tool_scope`
([`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md)) is **not** a third source of
rules; it is an input to the `ToolScope` stage of the enforcement chain below. Configuration (where a
rule comes from) and enforcement (how any rule is applied) stay separate: **two config levels, three
enforcement stages** — not a symmetric "three of each."

**How policy is enforced (the 3-stage chain, `_can_use_tool`):**

```
  tool_use ─▶  1. ToolScope         static allow/deny by tool name (no I/O)
              2. PermissionChecker  evaluate against the workflow rules + sensitive paths
                                    → allow · deny · "requires confirmation"
              3. PermissionHandler  only if "requires confirmation" → ask a human  (§7d)
```

Stages 1–2 are **mechanism + policy-as-data** (the workflow supplies the rules). Stage 3 is the
**human seam** ([§7d](#s7d)) — the escalation point when the rules defer to a human. This chain runs
for **every** provider: in-process providers call back into it directly; subprocess providers reach it
through the permission bridge ([§9](#s9)).

<a id="s7b"></a>
### 7b. Middleware — input and output

Middleware intercepts messages. Each middleware can **transform** the content or **reject** the turn
(veto). Two symmetric points:
- **Input** (`before_send`) — runs on the prompt before it reaches the provider: redaction,
  injection-detection (reject), context shaping.
- **Output** — runs on the model's response before it reaches the consumer.

**Where output middleware sits relative to the queue.** The provider's stream is buffered through the
per-turn `asyncio.Queue` ([§6](#s6)); output middleware runs on the **drain side** — as events come
*off* the queue, just before they are yielded to the caller. An **incremental** filter processes each
chunk on drain (and can cut the stream off mid-flight, e.g. on a detected leak); a **buffered** filter
accumulates the drained text and emits after the stream completes. Because the queue already decouples
producer from consumer, a slow output filter never stalls the provider — it only delays egress.

<a id="s7c"></a>
### 7c. Custom tools

An application can inject tools the agent may call — a `CustomTool` (`name`, `description`,
`input_schema`, `handler`). Two ways to deliver them, with different reach:
- **A list of `CustomTool`s** (in-process Python handler) — works for **in-process** providers
  (`claude` (SDK), `openharness`).
- **An MCP server** the provider connects to — works for **all** providers, including the
  **subprocess** ones (`claude-cli`, `codex`). MCP is the universal mechanism, and the right choice
  when a custom tool must be available regardless of provider transport.

<a id="s7d"></a>
### 7d. Permission handlers (the human seam)

Stage 3 of the chain is a `PermissionHandler` the **drive path** supplies. Each is a one-method
adapter that turns "the rules want a human" into an actual decision:

- **`AutoAllowHandler`** — allows every tool call; for fully-automated runs that opt out of approval.
- **`CLIPermissionHandler`** — prompts in the terminal (`y/N`); for the interactive CLI.
- **WebSocket / browser handler** — forwards the prompt to a browser and awaits the user; implemented
  by a web transport. *(protocol built-in; the browser adapter lives in the driving app.)*
- **Durable HTTP handler** — over the Runs API the confirmation is durable: the run pauses and resumes
  via `POST /runs/{id}/tool_confirmation` ([§11](#s11)), so approval survives the request/response
  boundary and a human who takes minutes to answer.

The handler holds **no rules** — it is only *how* a confirmation reaches a human once the checker has
already decided the call needs one.

<a id="s7e"></a>
### 7e. Resource governance — the fourth seam

The first three seams decide *what* a turn may say and do. The fourth bounds *how much* it may consume:
a per-`(user, task)` **cost cap and time deadline**. It is the exact analogue of the permission seam —
the engine reaches a **checkpoint**, asks the policy holder for a verdict, and obeys it without
understanding *why*:

| | Permission seam (§7a) | **Governor seam (§7e)** |
|---|---|---|
| Trigger | the agent emits a **tool call** | the run reaches a **checkpoint** (turn end · tool gate · clock tick) |
| Engine asks | `_can_use_tool(call)` | `governor.check(run, usage_delta, elapsed)` |
| Verdict | `allow · deny · ask-human` | `continue · stop(reason)` |
| Engine understands | nothing about *why* | nothing about dollars or seconds |
| Policy holder | workflow rules + `PermissionHandler` | the **`Governor`** (cost + time + counts), in `harness_api/` |

The **numbers never enter the engine** — only the verdict does. The cap, the budget unit (dollars,
token-proportional credits, or an opaque effort unit), and the ledger all live in the Axis-2 wrapper
([§8](#s8)), exactly where `KeyRegistry` and the `Governor`'s reservation ledger already live. Like the other seams,
governance is composed onto the harness, not welded in: a single-tenant deployment runs with **no
governor at all**, exactly as a harness without governance. The full model — capability tiers per provider, the
reserve→settle lifecycle — is [`06-resource-governance.md`](./06-resource-governance.md).

---

<a id="s8"></a>
## 8. Tenancy and isolation

### Two meanings of "multi-tenant"
"Multi-tenant" sounds like one feature, but it is really two independent ideas — and the harness
deliberately **owns one and pushes the other out** to the layer above it.

- **Isolation & addressing.** Hand the harness a `(user_id, task_id)` and it hands back an isolated
  workspace; run a hundred with distinct keys and they never touch each other. This is pure
  *mechanism*, so it lives in the **engine core**.
- **Accounts, billing, quotas.** *Which* key a user runs on, what budget they have, whether they are
  out of money. This is an opinion about identity and money — *policy* — that the engine should have
  no view on. So it lives one layer up, in the **Runs API wrapper** (`harness_api/`).

| | Isolation & addressing | Accounts, billing, quotas |
|---|---|---|
| Decides | which workspace; run many at once | which key, which budget, the cost/time caps |
| Kind | mechanism | policy |
| Lives in | the **engine core** | the **`harness_api/` wrapper** |
| Made of | `task_dir` / `workspace_key`, per-`(user,task)` locks, `Semaphore(N)`, session DB (resume by key), per-run `auth_env` | `KeyRegistry`, the **`Governor`** (reservation ledger for cost + time caps, §7e) |

So **the harness is tenant-*isolated* but account-*agnostic*.** It keeps everyone's work apart; it
holds no opinion on who they are or what they owe. The engine never sees a key registry or a dollar —
it only ever receives one resolved credential for one run.

The billing side (`KeyRegistry` + the `Governor`'s reservation ledger) — how a `user_id` becomes a key and a budget — is
detailed with the auth chain in [§10](#s10), since resolving *which* key and *injecting* it are one
story. The rest of this section is Axis-1: how isolation actually works.

### Where code runs (three tiers)
Three different things run in three different places — conflating them is the usual source of
confusion:

1. **Harness orchestration** — prompt assembly, the permission chain, streaming, persistence. Plain
   Python, running in **one shared process** on a **single async event loop**. That one thread
   interleaves *every* tenant's turns cooperatively (while one waits on its provider, another runs).
   It is safe to share because the harness never uses ambient state to know *who* it is serving —
   `(user_id, task_id)` is an explicit argument on every operation, and there is no per-tenant global
   state.
2. **The provider** — actually running the model + tool loop. **Where** this runs depends on the
   provider, and there are **three cases, not two**:
   - `claude-cli`, `codex` → the harness **spawns and owns an OS subprocess per run**: its own
     environment (with `auth_env` injected), its session home pinned into the task folder. Real OS
     isolation, and because the harness **holds the PID** it can **hard-kill** the run.
   - `claude` (SDK) → the SDK **also spawns the `claude` CLI as a subprocess** under the hood, and the
     harness controls that child's environment via `options.env`. So the model loop **is crash-isolated**
     (a child crash doesn't take the harness down) — but the **SDK, not the harness, owns the child
     PID**, so termination is *cooperative* (the SDK's `interrupt()`), not a harness-held hard-kill.
   - `openharness` → the only **truly in-process** case: it runs the loop **in the shared harness
     process** (per-instance `api_key`) and does its heavy compute in a **shared Ollama daemon** the
     harness didn't spawn. Per-run credential separation, but **no OS process boundary the harness owns**
     — hence no crash isolation and no hard-kill ([§9](#s9), [§10](#s10)).
3. **Tool execution** — the `Bash`/`Read`/`Write` the agent invokes. Runs wherever the provider runs,
   with `cwd` = the task folder, so file operations are scoped to the workspace either way.

### How two runs execute at the same time
Two runs with distinct `(user_id, task_id)` genuinely run **concurrently**. The mechanics:

- **Bounded, not unbounded.** A global `Semaphore(N)` caps how many run at once; the rest queue.
- **Serialized where it matters.** A per-`(user,task)` lock means one task never runs two turns at
  once, and within a session turns serialize (a new `send` cancels the in-flight stream). *Different*
  tasks proceed freely.
- **No global provider lock** — each run holds its own provider session, so they don't contend.

The subtle part is *how* two runs progress at once on a single-threaded event loop. The **Python
orchestration** (prompt assembly, permission checks, streaming glue) is interleaved cooperatively —
one thread, so only one run executes Python at any instant. But that is cheap coordination; the
**heavy agent work runs off the main thread**:

- **subprocess-backed providers** (`claude-cli`, `codex`, and the `claude` SDK's CLI child) run as
  separate OS processes → **true parallelism**;
- **`openharness`** runs in-process, its heavy work an awaited HTTP call to the Ollama daemon — which
  the event loop overlaps.

So while run A waits on its provider, run B's orchestration runs; and both providers do their heavy
work simultaneously off-thread. Two runs make real wall-clock progress at the same time — they are
**not each other's blockers**.

### Isolation is layered
**Five dimensions**, and the providers differ only on the fourth. The first four are **mechanism**
(Axis-1); the fifth is **policy** (Axis-2 — the fourth seam):

| Dimension | Enforced by | `openharness` (in-proc) | `claude` SDK (SDK-owned subproc) | `claude-cli` / `codex` (harness-owned subproc) |
|---|---|---|---|---|
| **Addressing / coordination** | threading `(user_id, task_id)` everywhere | ✅ | ✅ | ✅ |
| **Filesystem** | `cwd` = task folder + tool scope | ✅ | ✅ | ✅ (+ home pinned in) |
| **Credentials** | per-run injection point (`api_key` · `options.env` · subprocess env) | ✅ | ✅ | ✅ |
| **Crash isolation & hard-kill** | an OS process boundary; **harness-owned** ⇒ killable | ❌ neither | ✅ crash-isolated, but **no harness-owned kill** (cooperative `interrupt()`) | ✅ + **SIGKILL** |
| **Resource (cost + time)** *· policy* | the Governor seam ([§7e](#s7e)) | seam-enforced | seam-enforced | seam-enforced |

**Coordination, files, and credentials are safe on every provider** — each exposes a per-run credential
injection point that isn't process-global `os.environ` (`openharness`'s `api_key`, the SDK's
`options.env`, the subprocess env — [§10](#s10)). The axis that differs is **blast radius**: only
`openharness` is *truly* in-process, so a hard crash or runaway loop there takes the shared event loop —
and every co-resident tenant — down with it. The subprocess-backed providers (**including the SDK's CLI
child**) contain a crash; only the **harness-owned** subprocess (`claude-cli`/`codex`) additionally
gives a harness-held **hard-kill**. Cross to an owned process boundary when you need containment *and*
the ability to kill; the permission chain still reaches a subprocess through the bridge ([§9](#s9)),
so you don't trade away control to get it. Resource bounds (cost/time) are orthogonal — enforced by the
Governor seam on *every* provider, to the degree the provider allows ([§7e](#s7e), [`06`](./06-resource-governance.md)).

### Deployment (Docker + endpoints)
The Runs API container exposes HTTP; **each run is a per-run async task** (a `ChatAPI`) inside the
container's single process. Whether that run *also* spawns an OS subprocess **depends on the
provider** — `claude-cli`, `codex`, and the `claude` SDK all do (the SDK spawns its CLI child); only
`openharness` stays fully in-process. So the container is one process multiplexing every tenant's
orchestration; a per-run OS boundary exists for all but `openharness`, but a **harness-owned,
hard-killable** boundary only for `claude-cli`/`codex`. Per-tenant strong isolation beyond that
(container-per-tenant / gVisor) is a deployment choice.

> **Mental model:** isolate by **key** for coordination, by **injection point** for credentials, by
> **process** for blast-radius, by **container** for deployment. How far out you go per tenant is a
> function of trust — and the control (permission) seam reaches across the process boundary via the
> bridge ([§9](#s9)), so isolation never costs you enforcement.

---

<a id="s9"></a>
## 9. Uniting isolation and control

Two capabilities we want per run once looked like they pulled in **opposite directions** — because of
*where the permission callback lives*:

- **Isolation** — per-run credentials + crash/resource containment.
- **Control** — the permission chain (`ToolScope → PermissionChecker → PermissionHandler`) + HITL,
  which needs the provider to **call back into the harness at each tool call**.

The apparent conflict was that isolation seemed to demand a **subprocess** while control seemed to
demand an **in-process** callback. In the harness's model they are **not** opposed: **every provider
carries both**, because auth and the callback ride *different* channels, and a subprocess reaches the
callback through a **permission bridge**.

### How each provider gets both
| Provider | Transport | Per-run auth | Crash isolation | Hard-kill | Permission chain / HITL |
|---|---|---|---|---|---|
| `claude` (SDK) | in-proc API → CLI subproc | ✅ `options.env` | ✅ (child crash contained) | — cooperative `interrupt()` | ✅ in-process `can_use_tool` |
| `openharness` | in-process | ✅ `api_key` arg | — (shared Ollama daemon) | — | ✅ in-process permission bridge |
| `claude-cli` | subprocess | ✅ `auth_env` | ✅ | ✅ SIGKILL | ✅ `--permission-prompt-tool` → harness checker |
| `codex-mcp` | subprocess | ✅ `auth_env` | ✅ | ✅ SIGKILL | ✅ `mcp-server` `execCommandApproval` / `applyPatchApproval` |

> **Not yet shipped.** This is the *ideal-state* profile map. In the current code the shipped adapters are
> `claude` (SDK), `codex` (Codex Python SDK) and `openharness`; the standalone `claude-cli` and the reduced
> `codex-exec` keys are gated (`providers/__init__.py` raises `NotImplementedError` — the files are kept
> mv-only), and a distinct `codex-mcp` adapter is not present (its per-action gating rides the `codex` SDK
> adapter's approval bridge).

The axes are **orthogonal**. Per-run *auth* is available everywhere — the injection point just differs
by transport ([§10](#s10)). *Crash* isolation needs an OS process boundary, and the SDK and the
subprocess providers all have one — the SDK spawns a CLI child, so it too is crash-isolated; only
truly-in-process `openharness` lacks it. A **harness-owned hard-kill** is the stricter property: it
needs a boundary the harness *itself* spawns and holds the PID for — `claude-cli`/`codex`. These are
properties of *where the code runs*, not gaps. Reach for a harness-owned subprocess when you need both
blast-radius containment **and** the ability to kill a runaway.

**Providers are capability *profiles*, not one-per-vendor.** A single vendor can be wrapped by several
adapters at different points on the isolation × control curve — `claude` (SDK) vs `claude-cli`, and
`codex-mcp` vs `codex`. The matrix above lists the **full-control** adapter for each vendor. Some
vendors also expose a deliberately **reduced** profile: `codex` (`codex exec`) keeps per-run auth and
crash isolation but has **no per-action approval hook**, so it is a valid choice for *pre-authorized /
sandbox-only* runs that don't need to gate each call. Where a vendor's headless mode can't call back,
control comes from a **separate adapter that can** (`codex-mcp`), not a flag on the existing one. Pick
the profile per run, by how much control that run needs.

<a id="appendix-providers"></a>
### Choosing a profile — when to use each
The four providers sit at different points on the *isolation × control × auth × cost* curve:

| Provider | Reach for it when… | Auth / cost | Isolation |
|---|---|---|---|
| **`claude` (SDK)** | Anthropic-specific work; **fastest to ship**; you want hooks / sessions / MCP and the permission bridge **for free** | OAuth **or** API key (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`, [§10](#s10)) | crash-isolated (CLI child), but **cooperative kill** — the SDK owns the PID |
| **`claude-cli`** | you want Claude **and** a **harness-owned hard-kill** / wall-clock deadline, and don't mind building the permission bridge | same as the SDK — OAuth or API key | **harness-owned** subprocess (hard-kill); permission via the bridge |
| **`openharness`** | model-agnostic / open-source / local; portability over isolation | per-instance `api_key`; local Ollama → compute/time, not $ | in-process + shared Ollama daemon → **weakest** (no crash isolation, no hard-kill) |
| **`codex` / `codex-mcp`** | a **non-Anthropic** alternative on OpenAI/Codex credits; `codex-mcp` when you need per-action gating | OpenAI key / Codex credits | harness-owned subprocess (crash isolation + hard-kill) |

The `claude` SDK and `claude-cli` wrap the **same binary** with the **same auth** — so the real choice
between them is *permission-bridge-for-free* (SDK) vs *harness-owned hard-kill* (CLI), **not** cost or
OAuth availability. Both support personal OAuth and an API key alike.

### The in-process resolution (`claude` SDK)
The SDK keeps its permission callback **in-process** *and* drives a CLI subprocess whose environment
(`options.env`) the harness controls. The callback travels over a **separate in-process control
channel**, not the child's environment — so populating `options.env` with the run's auth ([§10](#s10))
isolates credentials per run **without** disturbing `can_use_tool`. The SDK therefore carries **per-run
auth and the full permission chain + HITL together, with no bridge** — the natural default for
multi-tenant in-process runs.

### The permission bridge (subprocess providers)
A subprocess provider already has auth + crash isolation; the bridge is what lets it **honor the same
harness checker** without moving the decision into the child. The decision stays in the main process
(the **control plane**); only an *approved* tool call proceeds inside the subprocess (the **sandbox**):

```
   MAIN PROCESS  (the harness = control plane)        SUBPROCESS  (one per run = sandbox)
   ─────────────────────────────                      ──────────────────────────
   · the permission chain  _can_use_tool              · the provider CLI / agent
     (scope → checker → handler, may ask a human)     · its own env (auth_env), home pinned in
   · session registry · middleware · egress           · tool execution (Bash/Read/Write)

   the agent emits a tool call ──────────────────────▶ needs a decision
      _can_use_tool  ◀──── permission bridge ──────────  (calls BACK across the boundary)
      allow / deny / ask a human  ─────────────────────▶ approved call runs; denied never crosses
```

The bridge is provider-specific but one shape — *decide outside, execute inside*:
- **`claude-cli`** — launched with `--permission-prompt-tool` pointed at a local stdio MCP server the
  harness runs; the tool's handler calls `_can_use_tool` and returns `{behavior: allow|deny,
  updatedInput?}`. The CLI passes the **tool arguments**, so the subprocess reaches argument-level
  parity with the in-process SDK.
- **`codex-mcp`** — a *second* Codex adapter (alongside `codex` = `codex exec`, which is retained as
  the reduced, auth-only profile) that runs `codex mcp-server` (JSON-RPC over stdio); the harness
  answers its `execCommandApproval` / `applyPatchApproval` requests by mapping each to `_can_use_tool`.
  `codex exec` can't call back, so control needs this separate adapter — not a flag on the existing one.

Either way the subprocess keeps its auth/crash isolation **and** gains the full chain, including HITL
escalation. A denied call never crosses the boundary, and the deny reason feeds back so the model
**re-plans** rather than aborts.

---

<a id="s10"></a>
## 10. Auth

Auth resolves at the **L1 provider boundary** ([§5](#s5)). `resolve_auth(provider, env)`
(`providers/auth.py`) is a pure function: given a provider and an env mapping (default `os.environ`),
it returns the auth vars that provider needs and are present.

| provider | auth vars |
|---|---|
| claude / claude-cli | `CLAUDE_CODE_OAUTH_TOKEN`, else `ANTHROPIC_API_KEY` |
| codex | `OPENAI_API_KEY` |
| openharness | none (local Ollama) |

**The environment is the seam.** Two ways in:
- **Default** — set the env/container vars; providers inherit them.
- **Per-run override** — pass `ChatAPI(auth_env={...})`, threaded into the provider for that run.

**Which providers can carry per-run (multi) auth — and the injection point each exposes:**
- **`claude-cli`, `codex` (subprocess)** — per-run `auth_env` is written into the subprocess env
  (inherited creds stripped first so they can't shadow the injected key).
- **`openharness` (in-process)** — takes an `api_key` **constructor argument**, so auth *is*
  separable at init — per-instance, not `os.environ` (it points at local Ollama with a dummy key, but
  the mechanism is real).
- **`claude` (SDK, in-process)** — the harness sets the run's auth into `options.env`, the environment
  of the CLI subprocess the SDK drives, stripping any inherited credential first so it can't shadow the
  injected one. Because the `can_use_tool` callback rides a separate in-process channel, this isolates
  credentials per run **without** touching the permission chain — the SDK carries **per-run auth *and*
  the full chain together** ([§9](#s9)).

### The billing wrapper: which key, and can they afford it
Before a run starts, the wrapper (Axis-2, [§8](#s8)) answers two questions the engine never sees —
*which key?* and *can they afford it?*

**`KeyRegistry` — which key.** The operator holds a handful of provider keys (usually one per user
**tier**) plus a map of `user_id → {key, budget}`:

```
  keys:   anthropic-standard → {provider: claude-cli, tier: standard, secret_env: ANTHROPIC_KEY_STANDARD}
          anthropic-pro      → {provider: claude-cli, tier: pro,      secret_env: ANTHROPIC_KEY_PRO}
  users:  u1 → {key: anthropic-standard, budget: $5}
          u2 → {key: anthropic-pro,      budget: $50}
```

The detail that makes this safe to commit to git: **the config never contains a secret.** A key entry
only names `secret_env` — the *env var* that holds the real key at runtime (mounted by the container).
So the who-gets-what map is just configuration; the actual keys live outside it. With no config at all
the registry is empty and every run simply inherits the process's own credential (the single-key dev
default).

**The `Governor`'s reservation ledger — can they afford it.** It prices each run's tokens (USD per 1M,
per model) and runs a **gate before the run is even spawned**: it **reserves** the worst-case cost up
front and settles to the actual afterward, so a run that can't be afforded is turned away up front, not
mid-run. (This folds in the role of the retired `SpendTracker`, whose accumulate-then-check gate was
allow-first — it admitted the very first oversized run; the reserve→settle model closes that N10 gap.)

The engine at the end of this only ever receives a resolved credential for one run — it never learns
that users, tiers, or budgets exist. You could swap this wrapper for a completely different identity
or billing system and the engine would not change a line.

### How the token gets injected — who's responsible, top to bottom
Two questions worth separating: *who holds the secret* and *who injects it*. It's a chain, and it
differs at the last step by provider:

```
  TOP — operator / container
     · secret env vars:    ANTHROPIC_KEY_PRO = sk-…          ← the real keys, set once at deploy
     · MANAGED_KEYS_JSON:  users → keys → secret_env         ← the map (no secrets in it)
        │
        ▼
  WRAPPER — Runner + KeyRegistry + Governor ledger           "which key? can they afford it?"
     · auth_env_for("u2") reads the secret from its env var  →  { ANTHROPIC_API_KEY: sk-… }
     · Governor ledger: reserve u2's worst-case cost — under their $50 cap? → yes
        │   auth_env — a dict, in memory only, never written to disk
        ▼
  ENGINE CORE — ChatAPI → Orchestrator                       (a pure conduit)
     · threads auth_env down; reads no secret, stores nothing
        │   provider_kwargs["auth_env"]
        ▼
  PROVIDER — the last mile
     · subprocess (claude-cli / codex):  child env = { os.environ − inherited creds } + auth_env
                                          → the child sees ANTHROPIC_API_KEY = u2's key
     · in-process (claude SDK):           options.env = { − inherited creds } + auth_env  (the SDK's CLI child)
     · in-process (openharness):          api_key constructor argument
```

**One model, one injection point per transport.** The secret is resolved once per run by
`KeyRegistry`, **propagated down** the call chain as the `auth_env` dict, and **injected at the
provider's boundary** — always stripping any inherited credential first so the operator's key can't
shadow the injected one:

- **subprocess (`claude-cli`, `codex`)** — set `env=` on the child process.
- **in-process `claude` SDK** — set `options.env`, the environment of the CLI child the SDK drives.
- **in-process `openharness`** — pass the `api_key` constructor argument.

None of these is process-global `os.environ`, which is exactly what lets concurrent runs carry
*different* keys. With no `KeyRegistry` configured at all, every run simply inherits the process
credential — the single-key dev default.

**Responsibility in one line each:**
- **Operator / container** — owns the real secrets (env vars) + the map; sets them at deploy, rotates
  by changing an env var. Secrets never live in code or config.
- **`KeyRegistry` (wrapper)** — decides *which* secret per run and reads it into an `auth_env` dict.
- **Engine core** — a conduit: carries the dict, holds no policy, persists nothing.
- **Provider (subprocess)** — the last mile: injects `auth_env` into the child's env, stripping
  inherited creds to prevent bleed.

**Reporting the credential in use — identify by tag, never by key.** A run reports *which* identity it
is using — a **tag / username** associated with the key, its source (OAuth / API key / managed key),
and a **redacted fingerprint** — but **never the key itself**. The registry maps a tag → a key;
introspection surfaces the **tag**, not the secret it was initialised with.

---

<a id="s11"></a>
## 11. The two drive paths (the top seam)

Two ways to drive the engine; same core, different **transport** and **permission mechanics**:

| | **In-process** (`import ChatAPI`) | **HTTP** (Runs API) |
|---|---|---|
| How | drive the engine in your process | `POST /runs` → consume the event stream (SSE/webhook) |
| **Human-in-the-loop** | **synchronous** — `PermissionHandler` blocks the turn on a human | **durable** — a run *pauses* at a tool call, releases its slot, and resumes via `POST /runs/{id}/tool_confirmation` keyed by `(run_id, tool_use_id)`; the ask goes out as a durable event, the answer is a separate call any worker can service |
| Multi-tenancy — keys, budgets, concurrency | **you own it** — your app runs its own instances and passes its own auth; there's no managed-key registry or spend cap on this path | **the Runner owns it** — it bounds total concurrency (`Semaphore(N)`), resolves each user's key from the `KeyRegistry`, and enforces the per-user spend cap ([§10](#s10)) |
| Best for | interactive dev, tight approval loops | trusted automation and long-running approved flows |

**Two shapes of HITL.** Approval works on **both** paths, in two shapes. **In-process** it is
*synchronous* — the `PermissionHandler` blocks the turn until the human answers; ideal for tight
interactive loops. **Over HTTP** it is *durable* — the run transitions to a `requires_action` state,
emits the ask as an event on the durable channel, and frees its slot; a human may take minutes or
hours, and any worker resumes the run when the `tool_confirmation` arrives ([§12](#s12)). The durable
shape is **idempotent on `(run_id, tool_use_id)`** and bounded by an **SLA timeout** so an unanswered
ask resolves to a default rather than pinning state forever.

---

<a id="s12"></a>
## 12. Verbs and external interaction

Each layer exposes a small, explicit verb set. An external system drives the harness through one of
the two **top-level surfaces** (in-process `ChatAPI`, or the HTTP Runs API); the lower verbs are
internal contracts.

**L1 — `AgentProvider` protocol** *(what every provider implements; internal)*
`start()` · `send(prompt) → event stream` · `stop()` · `close()`.

**L2 — `Orchestrator`** *(the engine; driven by a drive path, not usually external)*
`send_message(...) → events` · `resume_session(id)` · `abort()` · `check_session_status(id)` ·
`close()`. At each **checkpoint** (turn end · tool gate · clock tick) the turn loop consults the
**governor seam** ([§7e](#s7e)) and arms the run's deadline; a `stop` verdict ends the run with a
terminal **`stopped(reason)`** event.

**L4a — `ChatAPI`** *(in-process surface — how a Python app drives the harness)*
`init()` · `send(prompt, …) → events` · `resume(session_id)` · `close()`.

**L4b — Runs API** *(HTTP surface — how an external system drives the harness)*

| Verb | Endpoint | Does |
|---|---|---|
| create a run | `POST /runs` | start a background run; returns `run_id` |
| stream events | `GET /runs/{id}/events` | SSE stream of the run's typed events |
| status | `GET /runs/{id}` | snapshot: status, usage, cost |
| cancel | `POST /runs/{id}/cancel` | cancel an in-flight run |
| confirm a tool | `POST /runs/{id}/tool_confirmation` | approve/deny a paused tool call (durable HITL — [§11](#s11)) |

**How an external system interacts.** A product with its own frontend + job queue:

1. `POST /runs` with `{user_id, task_id, workflow, input, sink}` — `sink` is SSE or a webhook URL.
2. Consume the event stream — `session`, `token`, `tool_use`, `result`, `error`, `stopped` (a
   governance stop on budget/deadline, [§7e](#s7e)).
3. On completion, read `result` (usage + cost) and persist whatever it needs in its own DB.

No secrets cross in the request: the harness resolves the user's managed key from its registry by
the caller's `user_id` ([§10](#s10)).

---

<a id="s13"></a>
## 13. Part-by-part inventory

### Core mechanism (unambiguously harness)
| Part | Package | Does |
|---|---|---|
| Orchestrator | `orchestrator/orchestrator.py` | Per-turn lifecycle; the composition root (L2) |
| ChatAPI | `drive/api.py` | Python-native `init/send/resume/close` (L4a) |
| Session manager / index / DB | `orchestrator/session/` | Session lifecycle + resume across processes (SQLite) |
| Stream runtime | `orchestrator/stream_runtime.py` | Turn setup: session resolution, image prep, persisted-turn glue |
| Workflow loader | `workspace/workflow/loader.py` | Parse `.workflows/*.yaml`; **fail-closed** deny-baseline (L0) |
| Providers | `providers/`, `schemas/providers.py` | Run an AI system (claude / claude-cli / codex / codex-mcp / openharness) |
| Provider auth seam | `providers/auth.py` | Resolve credentials per run (`auth_env` or `os.environ`) |
| Tool scope | `schemas/tool_scope.py` | Per-turn allow/deny enforcement |
| Permission checker | `safety/permissions/` | Evaluate a tool call against workflow rules + sensitive paths |
| Persistence | `persistence/` | Restore/snapshot the task folder; local + S3; `(user,task)` keying (L3) |
| Bootstrap | `workspace/bootstrap.py` | Scaffold `.claude/{skills,agents}` from an allowlist (L0) |
| Event schemas | `schemas/events.py` | The typed output contract |

### Seams (protocols in `seams/`; the *decision* is supplied by the app)
| Seam | Protocol | App supplies |
|---|---|---|
| Permission handler | `seams/permissions.py` | How a confirmation reaches a human (AutoAllow / CLI / WebSocket — [§7d](#s7d)) |
| Middleware | `seams/middleware.py` | Input and output interception (transform / reject) |
| Custom tools | `seams/custom_tools.py` | Tools the agent may call (list or MCP) |
| Resource governor | `seams/governor.py` | The `continue`/`stop(reason)` verdict at each checkpoint ([§7e](#s7e)) — cost/time caps supplied by the app |

### Batteries (optional; not on the core path)
| Part | Package | Note |
|---|---|---|
| Safety classifiers | `safety/middleware/classifiers/` | regex, DeBERTa-ONNX, PromptGuard, LLM-judge, Ollama-guard |
| Safety experiments | `safety/experiments/` | Benchmark harness — dev tooling, not runtime |

### Cross-cutting services (L3 — wrap every run; each detailed in a companion)
| Part | Package | Does |
|---|---|---|
| Telemetry | `observability/telemetry/` | OTel (→ Tempo/Prometheus) + Langfuse emission — [`02-observability.md`](./02-observability.md) |
| Audit | `observability/audit/` | Per-run JSONL hook logs + report aggregation → permission config — [`04-audit.md`](./04-audit.md) |
| Safety pipeline | `safety/middleware/`, `safety/permissions/` | Input/output middleware + permission enforcement + experiments — [`03-safety.md`](./03-safety.md) |

### Policy wrapper (Axis-2 tenancy — the driving layer, not the engine)
| Part | Package | Does |
|---|---|---|
| `KeyRegistry` | `harness_api/credentials/keys.py` | Per-user key/budget map → resolves per-run `auth_env` |
| `Governor` (reservation ledger) | `harness_api/governance/` (`ledger.py`) | Per-`(user,task)` cost accounting + the pre-run budget gate — **reserves** worst-case cost up front, settles to actual after (the N10 fix that folded in the retired `SpendTracker`); also enforces time caps and answers the governor seam (`continue`/`stop`) — [§7e](#s7e), [`06`](./06-resource-governance.md) |

### Transports
| Part | Package | Does |
|---|---|---|
| Runs API app | `harness_api/app.py` | `POST /runs`, `GET /runs/{id}/events`, `GET /runs/{id}`, `POST /runs/{id}/cancel` |
| Runner | `harness_api/runner.py` | Concurrency, per-task locks, Governor budget gate, key selection, event adaptation |
| Egress | `harness_api/egress.py` | SSE / webhook delivery |
| CLI | `drive/cli.py` | Interactive terminal driver |

---

<a id="s14"></a>
## 14. Non-goals (what the harness deliberately excludes)

The harness stays a mechanism by refusing four kinds of policy, each reached through a seam:

- **Prompt framing** — how a turn is phrased is app policy; the app assembles the full prompt.
- **What a permission decision is** — the harness enforces and can ask a human; the rules come from
  the workflow manifest and the app's `PermissionHandler`.
- **Which safety filter to apply** — the middleware *pipeline* is core; specific classifiers are
  opt-in batteries.
- **Accounts, billing, quotas** — *which* key, *what* budget, *how much* a run may spend or how long
  it may take: Axis-2 policy in the Runs API layer ([§8](#s8), [§10](#s10)). The engine enforces the
  cap only as a `continue`/`stop` verdict through the **governor seam** ([§7e](#s7e)) — it never sees a
  dollar, a user, or a tier. Governance is *policy reached through a seam*, exactly like the other
  three; that is why it can be a seam without the numbers ever entering the engine.

The workflow manifest reflects this line: only `name`, `description`, `permissions` — never product
semantics.

---

<a id="s15"></a>
## 15. The harness in one line

**Isolate by key, run a provider in an isolated workspace under a fixed permission manifest, and
stream typed output.** Everything about *what* to run and *how much* it may consume — the prompt, the
permission decision, the filter, the cost/time cap — enters through four seams: **permissions,
middleware, custom tools, and resource governance.** Everything
else is the application's. Isolation and control, once in tension, are **united per provider**:
every provider carries per-run auth beside the permission callback (subprocess ones through a bridge),
and a run's cost and time are bounded by the governor seam.
