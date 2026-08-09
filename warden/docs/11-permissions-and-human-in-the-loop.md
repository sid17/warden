<a id="s0"></a>
# The Harness — Permissions & Human-in-the-Loop

> Companion to [`01-conceptual-model.md`](./01-conceptual-model.md). Permissioning is the harness's
> **first seam** ([`01` §7a](./01-conceptual-model.md#s7a)): before any tool the agent emits runs, the
> engine asks *"may this call proceed?"* and obeys the verdict — `allow · deny · ask-a-human` — **without
> understanding why**. The *why* is the app's: a workflow's rules and, when the rules defer, a human.
> This doc is the full model behind that seam — the verdict shape, the gate chain, and the two shapes of
> human-in-the-loop (the in-process **warm hold** and the run-pausing **durable HTTP** round-trip via
> [`POST /runs/{id}/tool_confirmation`](./01-conceptual-model.md#s11)). It is the *concept*; the
> [`guides/permissions/*`](#s10) runbooks are the *how-to-test*. Written, like its companions, as
> settled design.

---

<a id="s1"></a>
## 1. The idea in one sentence

**Permission is a seam: at every tool call the engine asks a callback whether the call may proceed and
obeys its `allow · deny · ask-a-human` verdict, while the engine itself knows nothing about the rules
that produced it or the human it may consult.**

The engine owns the *verbs* — pause at a tool call, deny it, hold it, resume it by injecting a decision
into the exact held call. The app owns the *nouns* — which tools, which files, which calls need a human,
and who that human is. Human-in-the-loop is not a separate feature; it is what the seam does when the
verdict is `ask-a-human` and the answer takes a while to come back.

---

<a id="s2"></a>
## 2. Where it belongs: mechanism vs. policy

The [mechanism/policy test](./01-conceptual-model.md#s2) places this cleanly. *Which* tools are allowed,
*which* files are writable, *which* calls must stop for a human — these are opinions about a specific
application's risk tolerance: pure **policy**. The act of pausing a call, holding it, and injecting a
decision back into it is **mechanism** — the same on every provider, indifferent to what the policy says.

So the split is exact:

| Layer | Owns | Lives in |
|---|---|---|
| **Mechanism (engine)** | the gate site, the verdict verbs (`allow`/`deny`/`ask`), the pause/hold/inject machinery, the fail-closed defaults | `orchestrator/`, `seams/`, `safety/permissions/`, `providers/` |
| **Policy (app)** | the workflow manifest (tool allow/deny/confirm, file globs, mode), *who* answers an `ask`, *how long* to wait | the workflow YAML + the `PermissionHandler` the app supplies |

The engine never learns *why* a call is denied — only that it is. Configuration (where a rule comes
from) and enforcement (how any rule is applied) stay separate, exactly as [`01` §7a](./01-conceptual-model.md#s7a)
states: **two config levels** (harness deny-baseline · workflow manifest) feed **the enforcement chain**;
they are not a symmetric "N of each."

---

<a id="s3"></a>
## 3. The permission seam — verdict shape and the `can_use_tool` callback

Policy reaches the engine through a **seam** — a callback the engine invokes and whose verdict it obeys
([`01` §7](./01-conceptual-model.md#s7)). This is the exact analogue of the Governor seam that
[`06` §3](./06-resource-governance.md#s3) describes, one row earlier in the same table:

| | **Permission seam (§7a)** | Governor seam (§7e) |
|---|---|---|
| Trigger | agent emits a **tool call** | run reaches a **checkpoint** |
| Engine asks | `can_use_tool(call)` | `governor.check(run, usage, elapsed)` |
| Verdict | `allow · deny · ask-a-human` | `continue · stop(reason)` |
| Engine understands | nothing about *why* | nothing about dollars/seconds |
| Policy holder | workflow rules + `PermissionHandler` | the Governor, in `harness_api/` |

**The verdict shape.** A human-facing decision is the `PermissionDecision` dataclass in
[`seams/permissions.py`](../seams/permissions.py) — `allowed: bool`, plus a `source`, a `reason`, an
`always` flag (remember-this-tool), and an optional `updated_input` (a handler may *mutate* the call's
args on allow, round-tripped into the SDK's `updatedInput`). A workflow-rule decision is a distinct
frozen `PermissionDecision` in [`safety/permissions/checker.py`](../safety/permissions/checker.py) that
adds `requires_confirmation` — the third verdict, the one that escalates to a human.

**The callback.** The engine's gate is `Orchestrator._can_use_tool` in
[`orchestrator/orchestrator.py`](../orchestrator/orchestrator.py), passed to every provider as
`can_use_tool=self._can_use_tool`. It is a *thin binding* — it extracts the pending call's `tool_use_id`
from the provider's context and delegates all logic to one place: `evaluate_tool_permission` in
[`orchestrator/permission_surface.py`](../orchestrator/permission_surface.py). (The older
`ToolScope → PermissionChecker → PermissionHandler` prose in `01` §7a describes the *shape* of this
chain; `evaluate_tool_permission` is the real gate.)

**The real gate chain** (`evaluate_tool_permission`, in order):

```
  tool call ─▶ 1. tool_scope        per-turn allow/deny by name, no I/O — cheapest deny
              2. AskUserQuestion    if the tool IS AskUserQuestion → forward to the
                                    handler, return answers via updated_input
              3. PermissionChecker  evaluate against workflow rules + sensitive paths
                                    → allow · deny · requires_confirmation
              4. PermissionHandler  ONLY on requires_confirmation → ask a human;
                                    on allow-always, checker.remember(tool)
```

The `PermissionChecker.evaluate` priority ladder inside step 3 is itself a chain
([`checker.py`](../safety/permissions/checker.py)): `sensitive path` deny → `tool_access.deny` →
`tool_access.confirm` (escalate) → session `remember` cache → `tool_access.allow` → `file_access` globs
→ read-only auto-allow → mode default (`CONFIRM` requires confirmation, `READ_ONLY` denies writes,
`AUTO` allows). This runs on **every** provider — in-process providers call it directly; subprocess
providers reach it through a permission bridge.

---

<a id="s4"></a>
## 4. Tool scope, custom tools, and the per-provider gating split

**Two ways to declare what's gated.**

- **`ToolScope`** ([`schemas/tool_scope.py`](../schemas/tool_scope.py)) — a per-turn allow/blacklist by
  tool name: `allowed` (whitelist wins) or `denied` (blacklist); neither set ⇒ everything passes. It is
  the *cheapest* deny (step 1, no I/O) and is a per-turn **input** to enforcement, not a third rule
  source — see [`05` §"Two things the harness still owns"](./05-app-interaction-patterns.md).
- **The workflow manifest** — the `PermissionChecker` derived once, at session bind time, from the
  workflow's `permissions` (`build_permission_checker` in
  [`permission_surface.py`](../orchestrator/permission_surface.py)). The workflow is *init-bound session
  identity*: a `send` that names a *different* workflow raises `WorkflowMismatchError` — changing it is a
  new-session act, not a live re-point.

**Custom tools** ([`seams/custom_tools.py`](../seams/custom_tools.py)) are app-registered `CustomTool`s
(`name`, `description`, `input_schema`, `handler`) exposed to the model. Gating them is where the
providers **diverge**, and the reason is physical, not a policy choice:

| | Regular tools | Custom tools |
|---|---|---|
| **Claude** | native `can_use_tool` seam | the SDK **shadows** `can_use_tool` for tools in `allowed_tools`, so custom tools need a **scoped `PreToolUse` gate** — [`providers/claude/session.py`](../providers/claude/session.py) `_build_custom_tool_gate`, matcher `^mcp__harness_custom__`, which recovers the bare name and re-enters the *same* `self._can_use_tool` seam |
| others | the provider's own hook path (e.g. OpenHarness `PRE_TOOL_USE`) | reached through the same hook path — no shadow to work around |

The Claude custom-tool gate **fails closed** (deny) on any internal error — it *is* the only runtime
gate for those tools, so an error must never silently un-gate them (LAW 4). This per-provider difference
is why the [tool-permission-gating guide](#s10) exists.

---

<a id="s5"></a>
## 5. Human-in-the-loop: two shapes

When step 4 escalates (`requires_confirmation`), the engine calls the app's `PermissionHandler`. What
that handler *does with the wait* is the only difference between the two HITL shapes. Both build on the
**same seam** and the **same join key** — the pending call's `tool_use_id` — and neither uses a **nudge**
(re-send a message and hope the model re-issues the call); both **checkpoint-and-inject**: capture the
call's id, pause *at that exact call*, resume by injecting the decision *into the held call*.

| | **Warm hold** (in-process) | **Durable HTTP** (cross-process) |
|---|---|---|
| Component | `DeferRegistry` ([`seams/defer.py`](../seams/defer.py)) | native SDK `defer` hook + `_HitlMixin` ([`_runner_hitl.py`](../harness_api/_runner_hitl.py)) |
| How it holds the pause | an in-memory `asyncio.Future` keyed by `tool_use_id` — the provider **blocks inside the seam** | the call is **persisted + ejected from memory**; the turn ends; nothing is held |
| Resume | a controller calls `reg.resolve(id, allow=…)` → sets the future | `store.resolve(...)` then re-drive; the SDK re-fires the hook for the same id |
| Persistence | none (dies on restart) | [`DurableDeferStore`](../seams/defer_store.py) — file (`FileDeferStore`) or Postgres ([`postgres_defer_store.py`](../seams/postgres_defer_store.py)) |
| Use when | approval is seconds-to-minutes, same process, a human is at the console | approval may take minutes-to-days, may land on a different process/replica |
| Multi-approval | native — two calls in one turn → two ids → resolved independently | one deferrable tool per turn (SDK constraint) |

Built-in helpers for the warm path: `AutoAllowHandler` (auto-allow everything, for CLI/scripts) and
`CLIPermissionHandler` (prompt `[y/N]` in the terminal), both in
[`seams/permissions.py`](../seams/permissions.py).

The durable path also ships `DurableDeferHandler` (a `PermissionHandler` that records + **deny-to-end**
ejects, then injects on resume) and `UnwiredDurableHandler` (a **fail-closed** placeholder: if a
`durable_http` run is ever driven *without* the Runner wiring its per-run handler, every consult is
**denied** with a loud reason — a misconfiguration refuses tools, it never silently permits them).

---

<a id="s6"></a>
## 6. The durable pause/resume cycle

The durable path turns a tool approval into a first-class **run lifecycle state**. A run pauses at
`requires_action`, survives a process exit, and resumes when a decision arrives over HTTP:

```
  POST /runs                         start a run (durable_http HITL configured)
      │
      ▼
  … agent works … hits a confirm-gated tool …
      │   PreToolUse defer hook records the pending call to the DurableDeferStore,
      │   ejects it from memory, the turn ends (no future held, process may exit)
      ▼
  status = requires_action           _maybe_pause_durable emits a replayable
      │                              `permission_request` event {tool_use_id, tool_name,
      │                              tool_input, reason, revise_round}; arms the SLA
      │
      │   … a human (maybe minutes/days later, maybe a different replica) …
      ▼
  POST /runs/{id}/tool_confirmation  {tool_use_id, decision, reason?, feedback?}
      │   confirm() → store.resolve(allow=…) → emit `permission_resolved` → re-drive
      ▼
  status = running (resumed)         the SDK re-fires the defer hook for the SAME
                                     tool_use_id → exact-id inject, no regeneration;
                                     the held call advances and the run continues
```

The route is `POST /runs/{run_id}/tool_confirmation` in
[`harness_api/app.py`](../harness_api/app.py) (`confirm_tool`), delegating to `Runner.confirm`
([`_runner_hitl.py`](../harness_api/_runner_hitl.py)).

**Decision-aware continuation.** A durable resume must *not* re-send the original task prompt — that
would make a multi-tool agent restart its plan (a "defer storm"). Instead `_execute` sends a *neutral*
continuation whose wording depends on the decision (`_run_state.py`): an **approve** says "proceed from
that point"; a **deny** says "do NOT retry it or an equivalent action" (a model told it "must" finish a
task would otherwise re-issue the denied call every resume — a live-bed finding); a **revise** attaches
the operator feedback and demands a *different* proposal.

**Idempotency & SLA (both in code).** `confirm` is idempotent on `(run_id, tool_use_id)`: a duplicate
returns `already_resolved` and re-runs nothing; a wrong/stale id returns `not_pending`
([`_runner_hitl.py`](../harness_api/_runner_hitl.py)). The `DurableDeferStore` enforces the same at the
record level — `pending → resolved → consumed`, and `get_decision` returns a resolved decision *once*
then marks it consumed ([`defer_store.py`](../seams/defer_store.py)). The **SLA** (`hitl.sla_seconds`)
arms a per-ask deadline; if it elapses the gate **expires** to a clean, distinct `permission_expired`
terminal (never a silent auto-deny, never a pinned run) — while `sla_seconds is None` means *indefinite*:
the ask stays durably parked until the human returns. And a confirm landing on a **different replica**
than the one that paused is cold-resumed by `_reconstruct_paused_run`, which rebuilds the paused
`_RunState` from the shared durable event log + the persisted spec.

---

<a id="s7"></a>
## 7. The provider split — why durable HTTP is Claude-only

Durable HTTP HITL is **Claude-only**; OpenHarness and Codex are **hard fail-closed** on it. This is not
a policy preference — it is what each provider physically supports:

```python
# harness_api/_run_state.py
_DURABLE_HTTP_UNSUPPORTED = ("openharness", "codex")
```

Only Claude's SDK has a **native `defer`** that re-fires the *same* `tool_use_id` on resume with no model
regeneration — true exact-id inject, so a multi-tool plan advances one held call at a time and converges.
OpenHarness and Codex have no native defer; their only HTTP option is **re-drive**, which must *restate*
the task to make the model re-issue the call — and restating breaks multi-tool convergence (each resume
restarts the plan → defer storm). So a `durable_http` run on those providers is rejected, in two places:

- **Pre-flight** in `_run` ([`_runner_exec.py`](../harness_api/_runner_exec.py)): a durable
  OpenHarness/Codex run is terminated up front with `_durable_http_unsupported_reason` — before the slot,
  before the provider factory.
- **Defense-in-depth** in `_wire_durable_eject` ([`_runner_hitl.py`](../harness_api/_runner_hitl.py)):
  wiring a durable eject for an unsupported provider raises rather than silently downgrading to a lossy
  re-drive handler.

The **warm hold** has its own, wider reach: it works wherever the provider awaits the async seam without
deadlock — Claude (`can_use_tool` / the custom-tool `PreToolUse` gate) and OpenHarness (`PRE_TOOL_USE`).
**Codex cannot even warm-hold** (its approval bridge is a sync reader-thread call with a hard timeout, so
holding pins the thread) — it must decline-to-end and re-drive. This provider matrix is documented in the
[provider-permission-behavior guide](#s10).

---

<a id="s8"></a>
## 8. The three-mode decision — approve / reject / revise

The durable confirm is not a binary allow/deny; it is a **three-mode gate** (E6). The wire schema is
`ToolConfirmation` in [`harness_api/schemas.py`](../harness_api/schemas.py): a `tool_use_id`, a
`decision: Literal["approve", "reject", "revise"]`, an optional `reason`, and a `feedback` field.

| Mode | On the wire | Continuation the model is told | Outcome |
|---|---|---|---|
| **approve** | `store.resolve(allow=True)` | "the approval is now in effect — proceed" | the exact deferred tool **runs**, run continues |
| **reject** | `store.resolve(allow=False)` | "the operator DENIED it — do NOT retry or attempt an equivalent" | the tool is **refused**, the run halts on that step |
| **revise** | `store.resolve(allow=False)` | "the operator asked you to REVISE — feedback: `{feedback}` — produce a *different* proposal and re-submit" | the model re-plans and **re-submits the same gate** (the revise loop) |

`reject` and `revise` are **both `deny` on the wire** — the store records `allow=False` for both; only
the resume *continuation* (keyed off `state.last_decision`) differs, which is why `_RunState` carries a
3-valued `last_decision` rather than a bool (a bool could not tell them apart). Two guardrails make the
revise loop safe (both in code): the schema **rejects a `revise` with empty `feedback` (422)** — the
model is never resumed empty (the #1 storm cause,
[`schemas.py`](../harness_api/schemas.py) `_require_feedback_for_revise`); and if a revise resume yields
a **byte-identical** proposal (model ignored the feedback), `_maybe_pause_durable` **hard-stops** with
`_DUPLICATE_REVISE_REASON` instead of pausing forever — the gate itself runs ungoverned, so this is the
only backstop against an infinite revise storm.

---

<a id="s9"></a>
## 9. What the app owns vs. what the engine owns (recap)

| Concern | Engine (mechanism) | App (policy) |
|---|---|---|
| The gate site | `_can_use_tool` on every provider | — |
| The verdict verbs | `allow · deny · requires_confirmation` | — |
| The rules behind a verdict | evaluates them (`PermissionChecker`) | **supplies** them (workflow manifest: tool allow/deny/confirm, file globs, mode) |
| Per-turn narrowing | enforces `ToolScope` | passes a per-turn `tool_scope` |
| Who answers an `ask` | calls the `PermissionHandler` | **is** the handler (warm registry, CLI prompt, durable HTTP approver) |
| Pause / hold / inject | the whole warm + durable machinery | picks the shape (warm vs. `durable_http`) and the wait |
| Custom-tool gating | the per-provider gate (incl. Claude's shadow workaround) | registers the `CustomTool`s |
| Idempotency / SLA / cold-resume | enforces them | sets `hitl.sla_seconds`, drives the confirm route |
| The `ask`'s three modes | applies approve/reject/revise continuations | decides which, and writes the revise `feedback` |

The invariant, unchanged: the engine never sees *why* — only the verdict.

---

<a id="s10"></a>
## 10. See also

This doc is the concept. The runbooks under
[`guides/permissions/`](./guides/permissions/) are the how-to-*test* — do not duplicate them:

- [tool-permission-gating.md](./guides/permissions/tool-permission-gating.md) — which tools the
  `can_use_tool` gate actually honors (regular vs. custom, per provider), and why custom-tool gating
  needs the Claude shadow workaround.
- [provider-permission-behavior.md](./guides/permissions/provider-permission-behavior.md) — Claude
  (ideal) vs. OpenHarness vs. Codex permission behavior, side by side.
- [hitl-defer-warm-vs-durable.md](./guides/permissions/hitl-defer-warm-vs-durable.md) — the two HITL
  strategies and when to pick each.
- [durable-hitl-over-http.md](./guides/permissions/durable-hitl-over-http.md) — pausing a run for a
  human across processes, end to end.
- [durable-hitl-testing.md](./guides/permissions/durable-hitl-testing.md) — how to run the durable HITL
  tests and current status.

Related numbered docs:

- [`01-conceptual-model.md`](./01-conceptual-model.md) — the seam model ([§7a](./01-conceptual-model.md#s7a))
  and the two drive paths ([§11](./01-conceptual-model.md#s11)) this doc sits inside.
- [`05-app-interaction-patterns.md`](./05-app-interaction-patterns.md) — why per-turn `tool_scope` and
  HITL confirmations stay harness mechanism while prompt framing moves to the app.
- [`06-resource-governance.md`](./06-resource-governance.md) — the Governor seam, the exact structural
  sibling of the permission seam.
