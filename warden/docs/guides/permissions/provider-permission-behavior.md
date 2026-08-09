# Provider permission behavior — Claude (ideal) vs OpenHarness vs Codex

**What this document is:** the single reference for how each provider handles tool
permissions and human-in-the-loop pause/resume, categorized along six dimensions.
**Claude is the ideal path** — it does all six cleanly, so it's described first as
the reference. OpenHarness and Codex each get their own section describing *where*
they diverge from that ideal and *why*.

Companion guides: [tool-permission-gating.md](./tool-permission-gating.md) (the
gating matrix + probe) and [hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md)
(the defer strategies + test commands). This doc is the conceptual map that sits
above both.

## The six dimensions

1. **Native (built-in) tools** — is a `can_use_tool` deny actually honored for
   the model's own tools (Write/Bash/Edit…)?
2. **Custom tools** — is a deny honored for harness-registered custom tools?
3. **Permission consulted in-process** — is the gate wired to *our* seam and
   consulted synchronously during the run (vs auto-approved/bypassed)?
4. **Warm (future) hold** — can we pause by holding the consult on an in-memory
   `asyncio.Future` and resolve it later, same process?
5. **Durable defer** — can we eject the pending call to disk, end the turn/process,
   and rehydrate later (cross-process)?
6. **Resume style** — on resume, does the **exact** deferred call continue (id
   preserved), or does the model **re-issue** it (new id, matched by content)?

## Summary matrix

| Dimension | **Claude (ideal)** | **OpenHarness** | **Codex** |
|---|---|---|---|
| 1. Native tools | ✅ gated (`can_use_tool`) | ✅ gated (`PRE_TOOL_USE`) | ✅ gated (`approval_handler`, exec/patch) |
| 2. Custom tools | ✅ gated (`PreToolUse` hook) | ✅ gated (registry → `PRE_TOOL_USE`) | ❌ ungated by design (no pre-exec seam) |
| 3. Consulted in-process | ✅ yes | ✅ yes | ✅ native; ❌ custom bypasses |
| 4. Warm (future) hold | ✅ yes | ✅ yes | ❌ **can't hold** (sync bridge) |
| 5. Durable defer | ✅ yes (native `defer`) | ✅ yes (deny-to-end) | ✅ native (decline-to-end); custom N/A |
| 6. Resume style | ✅ **EXACT-ID** (same `tool_use_id` re-fires) | ⚠️ re-drive + content-match | ⚠️ re-drive + content-match |

**One-line reading:** Claude is the only provider that is fully gated *and*
supports both warm + durable pause *and* resumes the exact call by id. The other
two gate well but diverge on *how the pause resumes* (they re-drive) — and Codex
additionally can't warm-hold and can't gate custom tools.

> **Enforced at the durable-HTTP transport (M6 · 07b, 2026-07-24):** rows 5–6
> above are raw *provider capabilities* — OH/Codex *can* deny-to-end and re-drive.
> But over the **Runs-API durable HTTP** path, re-drive **restates the task on
> resume**, which restarts a *multi-tool* plan every pause (a defer storm), so
> `durable_http` HITL is made **Claude-only and hard fail-closed for OH/Codex**
> (rejected pre-flight, not silently downgraded). OH/Codex HITL uses the in-process
> **warm hold** instead. See
> [durable-hitl-over-http.md](./durable-hitl-over-http.md) and
> the 07b durable-HITL provider-split build note (internal).

---

## 1. Claude SDK — the reference (ideal path)

Everything routes through one in-process seam and the SDK provides a native
`defer` primitive with exact-id resume. This is the behavior the others are
measured against.

- **Native tools** — `can_use_tool(tool_name, tool_input, context)` is consulted
  per call; a deny blocks the tool. Arg-level (sees the real path/command).
- **Custom tools** — registered as an in-proc SDK-MCP server and added to
  `options.allowed_tools`, which makes the SDK *shadow* `can_use_tool`. A scoped
  `PreToolUse` hook (`_build_custom_tool_gate`, matcher `^mcp__harness_custom__`)
  re-routes each custom call back through the **same** `can_use_tool` seam — so
  custom tools gate exactly like native ones (pre-07 · M9). No double-gate:
  native tools stay on `can_use_tool`, custom on the hook.
- **In-process** — yes; the decision is made synchronously in the run.
- **Warm hold** — yes; the async seam can park on a future
  ([`DeferRegistry`](../../../seams/defer.py)) and resolve by exact `tool_use_id`.
- **Durable defer** — yes, and this is the differentiator: a `PreToolUse` hook
  returns `permissionDecision:"defer"` → the run **stops**, the `ResultMessage`
  carries `deferred_tool_use{id,name,input}`, and the pending call is serialized
  to the on-disk transcript. Nothing is held in memory; the process can exit.
- **Resume — EXACT-ID.** A fresh process with `options.resume=<session_id>`
  rehydrates the transcript and **re-fires the same `PreToolUse` hook for the
  identical `tool_use_id`**, with **no model re-generation**. The hook returns the
  stored allow (optionally `updatedInput`, camelCase) or deny, and the exact
  deferred call runs or stays blocked. This is *true resume*: the id you stored is
  the id that resolves. **Proven live, cross-process, accept + reject.**
- **Constraints (SDK):** headless-only; **one deferrable tool per model turn**
  (so multi-approval is a warm-mode feature, not a durable-defer one);
  `permission_mode` must match on resume (we always use `"default"`).

**Why it's ideal:** one seam gates every tool, a native defer primitive ejects
cleanly, and the transcript-on-disk + hook-re-fire gives deterministic exact-id
resume across processes. Steps 1–6 all land.

---

## 2. OpenHarness — diverges on *id surfacing* and *resume style*

OpenHarness **gates as well as Claude** (both native and custom), and supports
warm + durable pause. It diverges in two places, both downstream of not having a
native `defer` primitive.

- **Native tools** — gated via the `PRE_TOOL_USE` hook (`_execute_tool_call`
  fires it with the full `{tool_name, tool_input}`); a deny blocks the tool. Same
  arg-level quality as Claude.
- **Custom tools** — registered in the tool registry, so they run through the
  **same** `PRE_TOOL_USE` gate as built-ins. **Gated by construction** (no shadow
  problem like Claude's — this is actually the simplest of the three).
- **In-process** — yes.
- **Warm hold** — yes; the `PRE_TOOL_USE` hook is async and can await our future.
- **Durable defer** — yes, via **deny-to-end**: an unresolved call is denied so
  the turn ends (`DurableDeferHandler`), the pending record is persisted, and the
  process can exit.
- **Resume — RE-DRIVE + content-match (divergence #1).** There is no native
  defer that preserves the exact call. On resume (`continue_pending()` /
  `load_messages`) the model **re-issues** the tool call, which gets a **new id**.
  So the stored decision is matched by `(tool_name, normalized input)`, not by the
  original id. Deterministic when the re-issued args are identical; drifts when a
  small model changes them.
- **Tool-id surfacing (divergence #2).** The real LLM id (`tc.id`) exists upstream
  and is even carried in the conversation history, but the `PRE_TOOL_USE` payload
  (`query.py:884`) **omits it** and the stream events strip it — so our seam never
  sees it. We **mint a stable harness id / use a content key** instead. `openharness-ai`
  is a pinned third-party pkg, so exact-id parity is a 1-line upstream change
  (add `tool_use_id` to the payload) or a mint-and-correlate away.

**Why it diverges:** OpenHarness gives us a clean in-process gate for *both* tool
types (its strong point), but lacks (a) a native defer primitive that freezes the
exact call, and (b) id exposure at the hook. So its durable resume is
"restart-the-call and re-attach the decision by content," not exact-id resume.

---

## 3. Codex — diverges on *warm hold* and *custom-tool gating*

Codex gates native (exec/patch) permissions fail-closed, but it is the most
constrained provider: it **can't hold a future**, and it **can't gate custom
tools at all**.

- **Native tools (exec/patch)** — gated via the SDK `approval_handler` run under
  the `untrusted` policy: a decline blocks the command/file-change. Fail-closed
  (any error/timeout → decline). Arg-level.
- **Custom tools** — **ungated by design (divergence #1).** Custom tools ride the
  MCP path, and the pinned Codex SDK (`openai_codex` 0.144.4) exposes **no
  answerable pre-execution MCP tool-call approval** — Guardian reviews arrive as
  *notifications*, and the only answerable seam would require our MCP server to
  emit a per-call approval elicitation from inside the tool wrapper at execution
  time (the rejected in-`_wrapper` half-gate). So custom tools fall back to the
  `codex_allow_ungated_custom_tools` policy: `True` = ungated auto-approve (loud
  warning), `False` = fail-closed (refuses to deliver them). See pre-07 §3d.
- **In-process** — native yes; **custom bypasses** the gate entirely.
- **Warm hold — NO (divergence #2).** The approval handler is a **synchronous
  reader-thread call** bridged to our async seam via
  `run_coroutine_threadsafe(...).result(timeout)` with a **hard timeout**. Holding
  the call open pins the reader thread and hits the timeout (fail-closed). There
  is no clean "hold open" primitive — so Codex is **durable-only**, never warm.
- **Durable defer** — yes for native: **decline-to-end** ends the turn cleanly,
  the pending record is persisted, the process exits. (Custom is N/A — no gate.)
- **Resume — RE-DRIVE + content-match.** `thread_resume` continues the thread; the
  model re-issues the call with a **new id**, matched by content. Same re-drive
  limit as OpenHarness. The real per-call id (`item_id`) *is* available for the
  durable **record** key, but the re-driven call's id differs.

**Why it diverges:** two hard boundaries — (a) the sync/async approval bridge with
a hard timeout means you cannot pause by holding, and (b) the MCP transport has no
pre-execution approval hook for tool calls, so custom tools can't be gated without
a half-gate we explicitly reject. Codex therefore supports the *durable* pattern
for native tools only, always via restart+content-match.

---

## How to think about it (why the divergences are what they are)

The whole picture reduces to **three capabilities**, and only Claude has all three:

| Capability | Claude | OpenHarness | Codex |
|---|---|---|---|
| A gate wired to our seam for **every** tool | ✅ | ✅ | native only |
| A way to **pause without re-generating** (native defer / holdable) | ✅ defer + warm | warm only (no native defer) | ❌ neither (sync bridge) |
| The **id survives** pause → resume | ✅ | ❌ (re-drive) | ❌ (re-drive) |

- **Claude is ideal** because the SDK ships a first-class `defer` primitive *and*
  keeps the transcript (with the pending call + its id) on disk — so resume is the
  exact call, by id, no regeneration.
- **OpenHarness gates the best** (native + custom, no shadow tricks) but has no
  native defer and hides the id → durable resume is restart+content-match.
- **Codex is the most constrained**: sync approval bridge (no warm hold) + no MCP
  pre-exec approval (custom ungated) → durable native-only, restart+content-match.

**Practical rule:**
- Need **exact-id, cross-process, crash-safe** approval → **Claude** (the only one).
- Need **short/interactive or multi-approval** → **warm hold** on Claude or
  OpenHarness (not Codex).
- On **OpenHarness/Codex durable**, expect **restart + content-match**: the stored
  `tool_use_id` is your record/API key, but the resumed call carries a new id and
  the decision re-attaches by content (reliable with stable args, weaker with
  small models).

## Verified status (2026-07-23)

- **Gating:** Claude native+custom GATED, OpenHarness native+custom GATED, Codex
  native GATED / custom ungated-by-design — live probe green.
- **Warm defer:** Claude exact-id inject (accept/reject/**multi**) + OpenHarness
  content-key inject (accept/reject) — live green.
- **Durable defer (cross-process):** Claude native-defer exact-inject accept +
  reject — live green (pass2 resumed the same session and injected across
  processes). OpenHarness/Codex durable = hermetic green; live eject+approve solid,
  re-drive content-match is model-dependent.

## Related

- [tool-permission-gating.md](./tool-permission-gating.md) — the gating matrix + reproduction probe.
- [hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md) — the two defer strategies + test commands.
- pre-07b-defer-implementation.md — the defer mechanic (§10 outcome, §11 warm-vs-durable + durable build).
- 07-durable-hitl.md — **M6**, the durable HTTP transport that wraps all of this.
