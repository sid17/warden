<a id="s0"></a>
# The Harness — Resource Governance

> Companion to [`01-conceptual-model.md`](./01-conceptual-model.md). Resource governance is the
> harness's **fourth seam** ([`01` §7e](./01-conceptual-model.md#s7e)) and its **fifth isolation
> dimension** ([`01` §8](./01-conceptual-model.md#s8)): given a `(user_id, task_id)`, it bounds a
> run's **money and time** — a cost cap and a deadline — and stops the run as proactively as the
> provider allows, **without the engine ever learning what a dollar or a user is.** This doc is the
> full model behind the seam — written, like its companions, as settled design.

---

<a id="s1"></a>
## 1. The idea in one sentence

**Resource governance is a fifth isolation guarantee: given a `(user_id, task_id)`, the harness
bounds the run's *money* and *time* — a hard cost cap and a hard deadline — and stops the run as
proactively as the provider allows, without the engine ever learning what a dollar or a user is.**

Tenant isolation keeps everyone's *files and keys* apart ([§8](./01-conceptual-model.md#s8)).
Resource governance adds: your *money* and your *time* are bounded too. A runaway tenant cannot
burn the operator's managed key or hold the event loop indefinitely.

---

<a id="s2"></a>
## 2. Where it belongs: policy, added at the edge

The [mechanism/policy test](./01-conceptual-model.md#s2) places this cleanly. A cost cap and a
deadline are opinions about **identity and money** — pure *policy*. The invariant from §8 holds:

> the harness is tenant-*isolated* but account-*agnostic* … the engine never sees a key registry
> or a dollar.

So the **numbers stay out of the engine.** The engine gains only *verbs* — reach a checkpoint,
stop a run, emit a typed terminal event, arm a deadline. The **Governor** (the component that holds
the caps and decides continue/stop) lives in the Axis-2 wrapper (`harness_api/`), alongside the
key registry and spend tracker it already has.

**This layer is *addable*, not load-bearing.** A single-tenant or hobby deployment runs with no
Governor at all. As a deployment scales to many paying tenants, it *adds* the
Governor (its own budget ledger, or a rented billing backend — see [§6](#s6)) without touching the
engine. Governance composes onto the harness; it is never welded into it.

---

<a id="s3"></a>
## 3. The Governor seam (a fourth seam)

Policy reaches the engine through a **seam** — a callback the engine invokes and whose verdict it
obeys without understanding ([§7](./01-conceptual-model.md#s7)). Three seams gate the *content* of a
turn — permissions, middleware, custom tools. Resource governance is the **fourth**, bounding a run's
*resources*, and it is the exact analogue of the permission seam:

| | Permission seam (§7a) | **Governor seam (§7e)** |
|---|---|---|
| Trigger | agent emits a **tool call** | run reaches a **checkpoint** (turn end · tool gate · clock tick) |
| Engine asks | `can_use_tool(call)` | `governor.check(run, usage_delta, elapsed)` |
| Verdict | `allow · deny · ask-human` | `continue · stop(reason)` |
| Engine understands | nothing about *why* | nothing about dollars/seconds |
| Policy holder | workflow rules + `PermissionHandler` | the **Governor** (cost + time + counts), in `harness_api/` |

The engine owns the verbs; the app owns the nouns (what the cap *is*, how much is too much). The
non-goal in [§14](./01-conceptual-model.md#s14) ("accounts, billing, quotas") is unchanged — the
*numbers* never enter the engine, only the verdict does.

---

<a id="s4"></a>
## 4. Resource isolation — a fifth dimension

§8 lists four mechanism isolation dimensions (addressing, filesystem, credentials, crash isolation &
hard-kill). Governance is the **fifth: resource isolation** — a per-tenant budget/deadline. It is
Axis-2 *policy*, but it *manifests* as an isolation guarantee, so it belongs in the same mental model:

| Dimension | Enforced by | Kind |
|---|---|---|
| Addressing / coordination | `(user_id, task_id)` threaded everywhere | mechanism (Axis-1) |
| Filesystem | `cwd`/`repo_path` = task folder + tool scope | mechanism (Axis-1) |
| Credentials | per-run injection point | mechanism (Axis-1) |
| Crash isolation & hard-kill | an OS process boundary; **harness-owned** ⇒ killable | mechanism (Axis-1) |
| **Resource (cost + time)** | **the Governor + its seam** | **policy (Axis-2)** |

Axis-2 grows from two components (`KeyRegistry` + `SpendTracker`) to **three** (+ `Governor`), and
the engine gains **one** seam.

---

<a id="s5"></a>
## 5. Two layers of budget, and per-provider reach

Two facts about provider capabilities shape what "enforce" can mean — and they are *physical*,
not a matter of how we write the code.

**Budget is already two layers, natively — the Governor is a third.**

| Layer | What it is | Example |
|---|---|---|
| **Soft (self-moderation)** | the model sees a budget and paces itself | Claude `task_budget` (a token countdown) |
| **Hard (native cap)** | the provider stops the call | `max_tokens` · `max_budget_usd` · `max_turns` · `num_predict` |
| **Governor (our enforcement)** | external per-`(user,task)` cap + deadline + reconciliation | the seam in §3 |

The Governor **composes with** the native layers — it does not replace them. For Claude it adds a
per-task ceiling *over* caps the provider already enforces; for the others it is the primary stop.

**Enforcement reach is tiered by provider — so the Governor needs capability tiers, not one path:**

| Provider | Usage visibility | Cancellation | Governor's real lever |
|---|---|---|---|
| **claude-code** | **mid-turn** cumulative output tokens | clean | price token-by-token, **stop mid-turn**; compose with native `max_budget_usd`/`max_turns` |
| **codex** | **mid-run, coarse** (cumulative session totals) | clean (kill) | our cap + process-kill; per-turn requires diffing totals |
| **openharness** (Ollama) | **terminal-only** | **unreliable** (server keeps generating) | cost isn't dollars — govern by **turn cap + wall-clock kill + `num_predict`**; accept post-hoc accounting |

The universal hard bound that works on *every* provider is the **turn/step cap + a wall-clock
deadline**. Dollar-precision mid-run is a Claude-only capability.

---

<a id="s6"></a>
## 6. The enforcement lifecycle — own the ledger, rent the billing

The Governor's mechanism is a three-stage lifecycle per run:

```
  PRE-FLIGHT   reserve min(remaining_credits, worst_case_cost) atomically
               worst_case = max_output_tokens × priciest allowed model
               reservation fails → stop(reason=budget) BEFORE the provider is called
      │
      ▼
  MID-RUN      hold the reservation; arm a wall-clock deadline (maxDuration);
               where the provider allows (claude), price the live stream and
               stop the instant the cap is crossed
      │
      ▼
  POST-HOC     settle actual usage, release the unused hold,
               emit the usage event to the durable ledger / billing backend
```

The decisive fact: **no billing platform enforces a hard cap in the request path,
and none ships a native reserve-then-settle primitive** — Stripe/Metronome/Orb/Lago/Polar are all
*meter → aggregate → signal* engines that say "you enforce." So the split is:

- **Own** the *synchronous reservation ledger* — an atomic check-and-reserve (Postgres row-lock or
  Redis Lua) so N concurrent runs on one tenant's budget cannot double-spend the same headroom. This
  is the one piece nobody sells; it is the fix for the concurrency-overshoot the whole field skips.
- **Rent** the *async source of truth* — a billing backend (self-hosted Lago, or Polar as a hosted
  merchant-of-record) for credit grants, reconciliation, and invoicing. Swappable behind the Governor.

Time governance falls out of the same lifecycle: the deadline is a mid-run arm + a stop. For a
**harness-owned** subprocess provider it is a clean OS kill (a *true* hard deadline); for the SDK and
truly in-process providers it is cooperative cancellation.

**On exhaustion, a run pauses — it does not silently die.** The hard cap is the floor: when a run
crosses its cost or time cap the Governor emits `stop(reason)` and the run ends. Where a top-up path
exists, that stop is a **resumable paused state** — the run releases its slot, emits the reason as a
durable event, and resumes when credits are added (idempotent on the run). Hard-stop is the guarantee;
pause-and-resume is the default wherever the deployment offers a way to add credit.

---

<a id="s6a"></a>
## 7. Where the check is injected — the practical map

[§5](#s5) says *what* each provider physically offers; [§6](#s6) says *when* in a run's life the
Governor acts. This section joins them: it is the **implementation view** — the fixed set of
checkpoint *sites* the engine exposes, and how each provider's granularity decides which of those
sites can actually stop a run, and how precisely.

**The tiers are already machine-readable.** Each provider session declares three capability flags
that the Governor reads to pick its enforcement path — it never branches on a provider name:

| Flag (on the provider `Session`) | claude | codex | openharness |
|---|---|---|---|
| `cost_visibility` | `"mid_turn"` | `"coarse"` | `"terminal"` |
| `supports_hard_deadline` | `False` (cooperative cancel) | `True` (OS kill of a harness-owned PID) | `False` (wall-clock + `num_predict`) |
| `max_output_tokens` | `None` (native compaction) | `None` (native compaction) | `4096` (harness-enforced window) |

Note the distinction the §5 prose blurs: **"clean cancellation" ≠ "hard deadline."** Claude's SDK
cancel is clean but *cooperative* (in-process — it can only stop at the next checkpoint); only
**codex** is a true OS kill, because it is a harness-owned subprocess PID. `supports_hard_deadline`
is the flag that encodes this, and it is why [§5](#s5) forbids a hard deadline on OpenHarness at the
seam.

**The checkpoint sites (provider-agnostic).** The engine offers the *same* injection points on every
provider; the Governor's verdict is obeyed at whichever ones the provider can feed:

| Site | Fires | What the Governor sees | Axis |
|---|---|---|---|
| **pre-flight** | before `session.send()` | reserved `worst_case` vs remaining headroom | cost |
| **tool-gate** | inside `_can_use_tool` (same point as the permission seam) | usage-so-far + elapsed | cost + time |
| **turn-boundary** | as the per-turn queue drains / on completion | terminal or cumulative usage + elapsed | cost + time |
| **clock-tick** | a wall-clock timer armed at run start | elapsed only | time |
| **mid-stream** | per usage delta *inside* a turn — **only where the stream reports it** | cumulative output tokens so far | cost |

**Per-provider realization — the finest granularity each actually permits:**

| Provider | Cost signal we read + where the hook sits | Finest **cost**-stop | **Time**-stop mechanism |
|---|---|---|---|
| **claude** | cumulative `output_tokens` off the SDK's **`message_delta`** stream, priced in the message handler → a **mid-stream** `governor.check()` | **mid-turn** — stop the instant the cap is crossed | cooperative cancel at the next checkpoint (no true kill) |
| **codex** | cumulative session totals on the **terminal `TurnResult`**, diffed per turn → a **turn-boundary** check | **between turns** — stop before the next turn, then PID kill | true **OS kill** of the subprocess, any instant |
| **openharness** | terminal usage on **`AssistantTurnComplete`** (post-hoc, not dollars) → a **turn-boundary** check | **next turn boundary** only (current generation finishes) | **wall-clock timer + `num_predict`** cap; no true kill — the server may keep generating |

The reading of this table: **the universal stop that works everywhere is the turn-cap + wall-clock
deadline** (fed by `turn-boundary` + `clock-tick`, present on all three). The extra precision —
stopping *mid-turn* on cost — exists **only on claude**, because only its stream reports cumulative
tokens mid-flight (the `mid-stream` site). Codex and OpenHarness have no mid-stream signal, so their
cost check can only land at a `turn-boundary`; the difference between them is *how* time is enforced
(codex = hard PID kill; openharness = cooperative wall-clock + a bounded `num_predict` window).

This is also why, within one session, the harness may complete **several turns and many tool calls
before it stops**: the Governor evaluates at each site it *can*, and only returns `stop(reason)` when
a cap is crossed there. Unbounded overshoot is still impossible — the **pre-flight** reservation
already bounded the whole run to `worst_case` before the first token, so the coarsest provider
overshoots by **at most one run**, never without limit.

> **Build note (updated — now shipped).** Claude's `mid-stream` site is **wired and bed-proven**
> (M2 §3c / B20a): the message handler threads cumulative `output_tokens` off the SDK's
> `message_delta` into a mid-stream `governor.check()`, and the T8c bed gate stops a real Claude turn
> mid-flight the instant the cap is crossed (see [§8](#s6b)).

---

<a id="s6b"></a>
## 8. Per-provider governance behavior — what you actually observe

§5 and §7 give the *design* tiers; this section states, per provider, what a deployment
**actually sees** — verified end-to-end on the Docker bed (the `--governance-budget[-codex|-oh]`
T8 gates: real **OAuth Claude + OAuth Codex + free Ollama**, no billed keys). **Governance is not
uniform across the three**, and two axes drive the difference: **pricing** (is the run costed in
dollars at all?) and **cost-visibility** (how proactively can it stop on cost?).

**The pricing axis — the biggest practical difference.** The default price table
(`DEFAULT_PRICING`) has rows for **claude-\*** only. A provider's *actual* cost is taken from its
own reported figure when it gives one (Claude's `total_cost_usd`), else priced from the table by
model-id prefix — and an **unknown model falls back to the default-model (opus) rate** rather than
zero (so a cap still bites). The net observed behavior:

| | claude | codex | openharness (Ollama) |
|---|---|---|---|
| Cost source | provider-reported `total_cost_usd` (**accurate**) | table lookup; model unknown ⇒ **default-model (opus) fallback rate** | terminal usage not costed ⇒ **$0** |
| Dollar cap + balance decrement | **yes, accurate** | **yes, but at the fallback rate** (add `gpt-*` to `PRICING_JSON` for the true rate) | **no** — the free local lane; governed by turns/wall-clock/`num_predict` |
| `worst_case` reservation | a **dollar** hold | **time/turn-only** (`pricing_shape="time_only"` — the model is not table-priced) | time/turn-only |

So **Claude is dollar-governed accurately; Codex is dollar-governed at a fallback rate until you add
its real prices; Ollama is not dollar-governed at all** (by design — the free lane). Note the
reservation and the running-cost meter can disagree for Codex (time-only *hold*, but a
fallback-priced *debit*) — pin the rate via `PRICING_JSON` to make both dollar-exact.

**The cost-visibility axis — how proactively it stops (from §7).**

| | claude | codex | openharness |
|---|---|---|---|
| `cost_visibility` | `mid_turn` | `coarse` | `terminal` |
| Finest cost-stop | **mid-turn** (message_delta tripwire) | **between turns** | next turn boundary (post-hoc) |
| Cost overshoot bound | ~one message_delta chunk | ~one turn | ~one full run |
| Deadline / kill | cooperative cancel | **true OS kill** (SIGKILL) | **forbidden** (`deadline_unsupported_on_provider`) |

**What is UNIFORM on all three** (the guarantees you always get): the pre-flight **reservation**
(no unbounded first run — overshoot ≤ one run), the **turn-cap** (`max_turns`), the **wall-clock**
tick, the `stop(reason)` + **pause-not-fail** path, and the meter-vs-enforce split
(`balance_backend` tracks; `allow_uncapped` blocks — money-uncapped never disables deadline/turns).

**Bed-proven (T8, real providers):**
- **claude** — T8a reject ($0); T8b funded turn priced + **balance decremented** ($0.0124: 5.0→4.9876);
  T8c low cap ($0.005) **stopped mid-turn**, incurred cost debited ($0.0429: 1.0→0.9571).
- **codex** — T8a reject; T8b/T8c **dollar-governed at the fallback rate** — balance decrements and
  the low cap stops it at the **turn boundary** ($0.072: 1.0→0.928).
- **openharness** — T8a reject; T8b/T8c **admit + balance flat** ($0) — the unpriced free lane,
  governed by turns/wall-clock, not dollars.

The gate asserts the provider-agnostic invariant `balance_after == balance_before − run_cost`
(settle debits exactly the committed cost), so it is correct whether the lane is priced or flat.

---

<a id="s7"></a>
## 9. What this deliberately does not change

- **The engine stays account-agnostic.** No dollars, seconds, users, or tiers enter it — only the
  Governor's `continue/stop` verdict. §14's non-goal is intact.
- **Governance is optional and pluggable.** No Governor → the harness behaves exactly as one without
  governance. The billing backend is a swappable dependency of the Governor, not of the engine.
- **The budget unit is the deployment's choice.** Dollars, token-proportional credits, or an
  opaque effort unit (ACU/checkpoint) — the engine never sees it; the Governor maps it.
