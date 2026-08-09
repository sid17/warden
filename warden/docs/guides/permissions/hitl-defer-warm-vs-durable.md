# Human-in-the-loop tool approvals — warm hold vs durable defer

**What this guide answers:** when the harness pauses a tool call to ask a human
*"allow or deny?"*, how does it hold that pause and resume with the decision —
and how does that differ when the approval comes back in **2 seconds** vs **2
days**? There are two strategies, and the right one depends on the delay.

Both build on the same seam (`can_use_tool` / the `PreToolUse` gate) and the same
join key (the pending call's `tool_use_id`). Neither uses the **nudge** (re-send a
message and hope the model re-issues the call) — that anti-pattern is retired.

> Prerequisite: custom-tool gating parity ([tool-permission-gating.md](./tool-permission-gating.md)).
> A tool has no `tool_use_id` at the seam unless the gate is consulted, so
> custom-tool defer needs the pre-07 gate first.

## The two strategies

### 1. Warm hold (future-based) — short, same-process

The permission consult parks on an in-memory `asyncio.Future` keyed by
`tool_use_id`; the provider blocks *inside the seam* (the tool does not run). A
controller resolves that future by id to inject the decision — the exact held
call then proceeds, deterministically, with no re-generation.

- Component: [`seams/defer.py`](../../../seams/defer.py) `DeferRegistry`.
- **Use when:** the approval is seconds-to-minutes, same process, a human is at
  the console, crash-safety isn't required. Simplest, lowest-latency, and it's
  the only path that cleanly handles **multi-approval** (two calls in one turn →
  two ids → resolved independently).
- **Liability:** holding a coroutine/Future across a multi-hour wait pins memory
  and dies on any restart/deploy/OOM. Do **not** use warm hold for long delays.

### 2. Durable defer (eject → rehydrate) — long delay, cross-process

On the consult, **persist the pending call, eject it from memory, end the turn**
(the process can exit). When the approval arrives later — possibly in a *fresh
process* — look the record up by `tool_use_id`, **rehydrate the session and
inject** the decision into that exact call.

- Components: [`seams/defer_store.py`](../../../seams/defer_store.py)
  `DurableDeferStore` (file-backed pending/decisions), plus either the Claude
  native-`defer` hook ([`safety/permissions/durable_defer_hook.py`](../../../safety/permissions/durable_defer_hook.py))
  or the [`seams/defer.py`](../../../seams/defer.py) `DurableDeferHandler`.
- **Use when:** the approval can arrive minutes-to-days later, cross-process or
  cross-machine, or a crash/deploy in between must not lose the pending call.
- **Store:** file-backed here (records keyed by `tool_use_id` **and** a content
  key; status `pending→resolved→consumed` for idempotency). M6 swaps in Postgres
  `run_events` behind the same shape.

The field standard (LangGraph checkpointer, Temporal signals, OpenAI Agents SDK
`RunState`, OpenHands event stream, Claude native `defer`) is universally
"persist + eject + rehydrate," never "hold a live process." See
pre-07b §11 (the defer-implementation build note, internal) for
the full field survey.

## Per-provider mechanism (3 providers × {built-in, custom})

Two resume styles: **exact-inject** (re-fire/resolve the SAME `tool_use_id`, no
re-generation) vs **re-drive + content pre-seed** (the resumed call mints a NEW
id, matched by `(tool_name, normalized input)`).

| # | Cell | Warm hold | Durable defer | Resume style |
|---|---|---|---|---|
| 1 | **Claude built-in** | future keyed by `context.tool_use_id` | native `defer` hook → `deferred_tool_use` + `options.resume` | **EXACT-inject** |
| 2 | **Claude custom** | future via the pre-07 `PreToolUse` gate | same native-`defer` hook (matcher `None` covers custom too) | **EXACT-inject** |
| 3 | **OpenHarness built-in** | future via `PRE_TOOL_USE` | `DurableDeferHandler` deny-to-end + `continue_pending()` | re-drive + content |
| 4 | **OpenHarness custom** | same as #3 (registry → `PRE_TOOL_USE`) | same as #3 | re-drive + content |
| 5 | **Codex built-in** | ⚠️ can't warm-hold (sync bridge deadlocks) | `DurableDeferHandler` decline-to-end + `thread_resume` | re-drive + content |
| 6 | **Codex custom** | **N/A** — pre-07 shipped the ungated fallback (no gate) | **N/A** | — |

Notes:
- **Only Claude gives cross-process EXACT-inject** (the SDK re-fires the hook for
  the same `tool_use_id` with no model regeneration). Everything else re-drives
  and content-matches — reliable when the re-issued args are identical, weaker
  when a small model drifts the args.
- **Claude `defer` constraints (SDK-verified):** headless-only; **one deferrable
  tool per model turn** (multiple tool calls in one turn ignore `defer` with a
  warning — so **multi-approval is a warm-mode feature**); `permission_mode` must
  match on resume (we always use `"default"`); `updatedInput` is camelCase on the
  hook output.
- **OpenHarness id:** the payload doesn't surface the LLM `tool_use_id` (upstream
  `query.py:884` omits it), so the durable store keys OH calls by content. The id
  exists upstream — a 1-line upstream change would give exact ids (see pre-07b §8).
- **Codex custom is ungated by design** (pre-07 §3d) — no gate, so nothing to
  defer.

## How to test

### Hermetic (no model)

```bash
uv run --no-sync python -m pytest \
  warden/tests/seams/test_defer.py \
  warden/tests/seams/test_defer_store.py \
  warden/tests/seams/test_durable_handler.py \
  warden/tests/safety/test_durable_defer_hook.py -q
```
Covers DEFER-1..4 warm (capture/pause/inject/multi-approval), the durable store
(cross-instance persistence, consume-once idempotency, content re-drive lookup),
the durable handler (eject/inject), and the native-defer hook (defer/allow/deny/
fail-closed).

### Warm, live (single process)

```bash
S=/tmp/hitlproof
# Claude — exact-id inject, accept / reject / multi (a.txt allowed, b.txt denied)
env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$OAUTH" PYTHONPATH=. uv run --no-sync python \
  warden/scripts/hitl-defer-resume-probe.py --provider claude --case accept --base $S
# OpenHarness — free Ollama
PYTHONPATH=. uv run --no-sync python \
  warden/scripts/hitl-defer-resume-probe.py --provider openharness --model qwen3:8b --case reject --base $S
```

### Durable, live (TWO subprocesses — the cross-process proof)

Each phase is a **separate process invocation**; they share only the on-disk
store + session DB (never memory):

```bash
S=/tmp/durableproof
P="env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN=$OAUTH PYTHONPATH=. uv run --no-sync python warden/scripts/durable-defer-probe.py"
$P --provider claude --case accept --base $S --phase pass1     # drive → eject → exit
$P --provider claude --case accept --base $S --phase approve   # (later, other process) write the decision
$P --provider claude --case accept --base $S --phase pass2     # fresh process → rehydrate → inject → tool runs
```

Signals: `pass1` records a non-null `tool_use_id` and exits with the tool NOT run;
`pass2` resumes the **same session id** and injects the decision (`out.txt`
present on accept, absent on reject).

## Verified status (2026-07-22)

- **Warm:** Claude built-in exact-id inject accept/reject/**multi** — live PASS;
  OpenHarness built-in content-key inject accept/reject — live PASS.
- **Durable:** Claude native-`defer` exact-inject **cross-process** accept +
  reject — live PASS (pass2 resumed the same session and injected across
  processes). OpenHarness/Codex durable re-drive — hermetic PASS; live, eject +
  approve are solid, the re-drive content-match is model-dependent (small models
  drift the re-issued args — the documented re-drive limit; exact-inject on
  Claude avoids it).

## Related

- [tool-permission-gating.md](./tool-permission-gating.md) — the gate every tool
  flows through (prerequisite).
- pre-07b-defer-implementation.md
  — the defer mechanic (§10 outcome, §11 warm-vs-durable field survey).
- 07-durable-hitl.md — **M6**,
  which wraps this durable mechanic in the HTTP transport (`run_events` +
  `requires_action` + `POST /tool_confirmation`) and swaps the file store for
  Postgres.
