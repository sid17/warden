# The Harness — Safety Model

> Companion to [`01-conceptual-model.md`](./01-conceptual-model.md). Safety in the harness is **defense in
> depth assembled from mechanisms the harness owns and policy the app supplies** — it is not one
> filter and it is not a subsystem of its own. This doc says what the layers are, which are mechanism
> vs policy, where each plugs into the [three content seams](./01-conceptual-model.md#s7), and how a safe
> configuration is *discovered by measurement* rather than guessed.

---

## 1. The principle, applied to safety

Reuse the governing test (concept note [§2](./01-conceptual-model.md#s2)):

- The harness owns the **mechanism** — a pipeline that can inspect the input, gate every tool call,
  and filter the output.
- The app owns the **policy** — *which* patterns are adversarial, *which* tools are allowed, *what*
  must never leak.

Safety is where that split is easiest to get wrong, because "make it safe" sounds like one feature.
It is really **four distinct jobs**, each defeating a threat the others cannot touch. The harness
stays a mechanism precisely because it never decides *what* is unsafe — it only provides the places
where an app's safety policy attaches.

The threats being defended against (the product-facing five, from the safety kickoff):

| # | Threat | Example |
|---|--------|---------|
| 1 | Tool access too broad | a reader accidentally triggers a `Write`/`Bash` |
| 2 | Internal exposure | "what skills do you have?" leaks skills, agents, system prompt |
| 3 | Intent manipulation | user ignores the workflow, drives the model off-task or adversarially |
| 4 | No execution isolation | a compromised model has the full system's access |
| 5 | Raw developer UI | users see internal machinery instead of a guided experience |

---

## 2. Four layers, re-slotted onto the seams

The safety work was *discovered* as "four layers" (structural absence, tool whitelist, input
middleware, output middleware) across 16 experiments. In the ideal model those layers are not a new
subsystem — **they are the three content seams plus the prompt, used well:**

| Layer | Defeats | Where it lives (concept note) | Mechanism / policy |
|---|---|---|---|
| **Structural absence** — a minimal custom system prompt; the model never receives skill/agent/tool names | #2, #3 | **Prompt framing** — app policy ([§14](./01-conceptual-model.md#s14) non-goal; assembled per [app-interaction-patterns](./05-app-interaction-patterns.md)) | policy (app) |
| **Tool whitelist** — register only the tools the task needs | #1, #4 | **Permission seam** ([§7a](./01-conceptual-model.md#s7a)) — `ToolScope` static gate + workflow allow/deny | mechanism + policy-as-data |
| **Input middleware** — inspect the prompt before the provider sees it; reject or redact | #3, #2 | **Input middleware seam** ([§7b](./01-conceptual-model.md#s7b)) | mechanism + policy (detectors) |
| **Output middleware** — inspect the response before the consumer sees it; filter or cut | #2 | **Output middleware seam** ([§7b](./01-conceptual-model.md#s7b)) | mechanism + policy (detectors) |

Two findings from the experiments are worth stating as durable design guidance:

- **Structural absence beats structural restriction.** The strongest anti-leakage lever is that *the
  model can't reveal what it never received* — a minimal prompt outperforms output filtering, tool
  restriction, or "don't reveal your instructions" rules. This is app prompt policy, not an engine
  feature — which is exactly why the harness pushes prompt framing out ([§14](./01-conceptual-model.md#s14)).
- **Tool whitelisting is the only *hard* guarantee.** A tool the provider never receives cannot be
  called by any jailbreak. Everything else (prompts, middleware) is probabilistic; the permission
  seam is mechanical.

The reconciliation is the whole point: **there is no separate "safety module" in the ideal model.**
Safety is what you get when the prompt, the permission seam, and the two middleware seams are
configured well. That keeps the engine a mechanism.

---

## 3. Input middleware

**Mechanism (harness).** The `before_send` pipeline: an ordered list of middleware, each of which may
**transform** (redact) or **reject** (veto the whole turn) before the prompt reaches the provider.
Runs in-process ([§7b](./01-conceptual-model.md#s7b)).

**Policy (app).** *Which* detectors, in *what* order. The ideal shape is a **cascade** — cheap to
expensive, short-circuiting on a confident block:

```
prompt ─▶ regex / phrase match ─▶ small trained classifier ─▶ LLM judge ─▶ provider
             (µs, brittle)          (~10ms, robust)          (~200ms)     (only if clean)
```

The detectors are **swappable batteries** ([§13](./01-conceptual-model.md#s13)), not core — enhanced
regex, an intent classifier, a sanitizer, PromptGuard-22M/86M, DeBERTa-ONNX, Ollama-guard, and an
LLM-judge, all interchangeable at the same seam.

The production default is a **trained cascade** — regex → DeBERTa/PromptGuard → Haiku-judge — not
brittle substring matching: same `before_send` interface, robust detection across rephrasing and
encoding. `safety/experiments/` holds the comparative evaluation that picks the cascade members (§5).

---

## 4. Output middleware

**Mechanism (harness).** Symmetric to input, but it runs on the **drain side** of the per-turn queue
([§6](./01-conceptual-model.md#s6), [§7b](./01-conceptual-model.md#s7b)) — as events come *off* the queue,
just before egress. Two shapes:

- **Incremental** — a rolling buffer (e.g. a 200-char window) filters each chunk as it drains and can
  **cut the stream mid-flight** on a detected leak. Streaming-compatible.
- **Buffered** — accumulate the full response, filter post-hoc. Simpler; adds latency.

Because the queue already decouples producer from consumer, a slow output filter never stalls the
provider — it only delays egress.

**Policy (app).** *What counts as a leak* — skill/agent names, `.claude/` paths, YAML frontmatter,
absolute user paths, or the canary (§5). A zero-cost **canary token** (a synthetic string planted in
the system prompt, checked in every chunk) is the backstop for verbatim prompt leakage.

Output filtering is a **first-class core pass**, not a driver add-on — so every drive path
([§11](./01-conceptual-model.md#s11)) inherits it, not just the CLI.

---

## 5. How safety policy is *discovered* — the experiments system

You do not guess the safe configuration; you **measure** it. The harness ships an **experiment
harness** (`safety/experiments/`) as dev tooling ([§13](./01-conceptual-model.md#s13) batteries — not on
the runtime path). Two kinds:

- **Preset experiments** — interactive sessions testing a *combination* of prompt + tool scope +
  middleware against an attacker. Answer: *"does this combination hold?"*
- **Classifier experiments** — automated evaluation of detectors against a **labeled corpus**
  (`safety/dataset/`) of adversarial/benign inputs and leaked/clean outputs. Answer: *"which detector
  detects best, at what latency?"*

Findings flow into production: a preset that holds becomes a **workflow manifest** (tool/file
permissions) plus a **middleware selection**; a winning classifier becomes the deployed cascade
member. Nineteen presets — five baselines (`unrestricted`, `ask-only`, `note-taking`, `prompt-guard`,
`layered`) plus a non-contiguous `e`-series (`e1`…`e15`, with `e1b`/`e2b` variants; no `e0`) — and ten
classifiers have already been run; results live in `safety/experiments/results/`.

One experiment result shapes the whole enforcement design and is worth carrying forward: the SDK's
`can_use_tool` callback **does not fire for auto-allowed tools like `Read`** — so per-*path* Read
restriction is impossible through the permission callback and must be done with a **`PreToolUse`
hook** instead. This is why path enforcement lives in hooks, and it is the bridge to
[`04-audit.md`](./04-audit.md), which derives those path hooks from observed behavior.

---

## 6. The safety pipeline, end to end

```
  user input
     │  ┌─ input middleware cascade ───────────────── reject → error   (§7b seam)
     ▼  │   regex → classifier → LLM-judge
  [ prompt framing: structural absence ]              ← app policy (§14)
     │
     ▼
  tool call ─▶ permission chain ─▶ allow · deny · confirm   (§7a seam; whitelist = hard block)
     │           ToolScope → PermissionChecker → PermissionHandler
     ▼
  provider runs
     │
     ▼
  response ─▶ output middleware ─────────────────────── filter / cut   (§7b seam)
     │           incremental (cut mid-stream) | buffered
     ▼
  canary check (zero-cost verbatim-leak backstop)
     │
     ▼
  consumer
```

Every box is one of the three content seams or the prompt. Nothing here is a bespoke safety engine.
(Resource governance is the fourth seam — [`01` §7e](./01-conceptual-model.md#s7e) — but it bounds
cost/time, not content, so it plays no part in the safety model.)
