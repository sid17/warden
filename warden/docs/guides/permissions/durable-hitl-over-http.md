# Durable HITL over HTTP — pausing a run for a human, across processes

> **Status (2026-07-24): finalized to the 07b provider split.** Durable HITL over
> HTTP is **Claude-only** — Claude has a native `defer` primitive (exact-id, resumes
> by *continuing* the paused conversation), so a multi-tool run converges. OpenHarness
> and Codex have **no** native defer; their only HTTP option is re-drive, which must
> **restate** the task on resume and so **breaks multi-tool convergence** (each resume
> restarts the plan → a defer storm). They are therefore **hard fail-closed** on this
> path — use the in-process **warm future hold** instead
> ([hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md)). Rationale +
> the live findings that forced this: the durable-HITL provider-split build note (internal) §2.

**What this guide answers:** when a run on the **Runs API** hits a tool that needs
a human's *"allow or deny?"*, how does the run **pause** without pinning a worker,
survive minutes-to-days (even a control-plane restart), and **resume** when the
decision finally arrives over HTTP? This is the transport layer (M6) that wraps the
in-process defer mechanic ([hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md))
into the actual `POST /runs` → `requires_action` → `POST /runs/{id}/tool_confirmation`
cycle.

If [hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md) is *"how do you
hold one pending call,"* this is *"how does a **server** hold a paused **run** and
resume it from a separate HTTP request."*

> Prerequisites: the same two — the [gate](./tool-permission-gating.md) every tool
> flows through, and the [defer mechanic](./hitl-defer-warm-vs-durable.md) this
> transport reuses. Conceptual map: [provider-permission-behavior.md](./provider-permission-behavior.md).

## The cycle (one durable HITL round-trip)

```
POST /runs {provider:"claude", permissions.handler:"durable_http"}   → 202, takes a slot, starts
  provider reaches a confirm-required tool
  → durable eject (Claude SDK-native defer): the turn ENDS at that tool_use_id
  → Runner reads the store, finds a NEW pending record
  → emits  permission_request  on run_events        (durable, replayable)
  → status = requires_action                         (the pause)
  → the slot is RELEASED — no worker pinned          (a 2nd run proceeds)
… minutes / days / a restart later …
POST /runs/{id}/tool_confirmation {tool_use_id, decision}
  → record the decision in the store
  → emit  permission_resolved                        (allow | deny | timeout)
  → RE-DRIVE the same run (same session_id) with a NEUTRAL CONTINUATION, not the
     restated task — the SDK re-fires the SAME tool_use_id (EXACT-inject) and the
     model CONTINUES from the held call (it does NOT restart the plan)
  → allow → the tool runs; the model proceeds to its NEXT tool (which pauses in
     turn — one confirm per tool), until the run completes (succeeded)
    deny  → the tool never runs, reason fed back, the model continues/reports
  (idempotent on (run_id, tool_use_id): a duplicate confirm is a no-op)
  (SLA: an unanswered ask auto-resolves to DENY — never a pinned run)
```

The durable ask + its resolution ride `run_events` like any event, so the pause
**survives teardown** and is reconstructable by replay — not an out-of-band channel.

### Multi-tool convergence — the thing that makes this real (07b)

A real agent does several tools in sequence (`pwd → write → verify`). On resume the
Runner must send a **neutral continuation** (*"continue where you left off; the
pending decision is in effect; do not restart"*), **not** the original prompt.
Re-sending the task makes the model start over on every resume — a defer storm that
never converges (the bug 07b fixed). With the continuation, Claude's SDK re-fires
the deferred `tool_use_id`, the model injects the decision into *that* call and moves
to the next tool. So an N-tool run pauses N times and completes in **N bounded
confirms**, each tool running exactly once.

## Why this is Claude-only — the one thing that matters

The whole split is one property: **does the provider preserve the pending call's
identity across pause→resume?**

| | Native defer? | Resume | Durable HTTP verdict |
|---|---|---|---|
| **Claude** | ✅ SDK `permissionDecision:"defer"` | `options.resume` re-fires the **same `tool_use_id`** and the model **continues** | ✅ **Supported** — exact-inject, multi-tool convergent |
| **OpenHarness** | ❌ | re-drive: model **re-issues** the call (new id) after a **restated** task | ❌ **Fail-closed** — restating breaks multi-tool convergence |
| **Codex** | ❌ | re-drive via `thread_resume` (new id); *also* can't warm-hold (sync bridge + hard timeout) | ❌ **Fail-closed** — same, doubly constrained |

- **Only Claude preserves the id** *and* can resume by **continuing** rather than
  restating. That combination is what makes durable HTTP sound, so Claude is the only
  supported provider and the hard gate.
- **OpenHarness/Codex** would have to restate the task to make the model re-issue the
  deferred call — and restating restarts a multi-tool plan every resume. This is a
  property of those SDKs lacking a native defer, **not a bug we can close** — so the
  Runner **rejects** a `durable_http` run for them (fail-closed) rather than silently
  degrade. Their HITL is the **in-process warm hold** ([hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md)):
  OpenHarness holds a future on `PRE_TOOL_USE`; Codex is short-approvals-only (its
  bridge is a synchronous reader-thread call with a hard timeout).

### Fail-closed, not silent-downgrade

A `POST /runs {provider:"openharness"|"codex", permissions.handler:"durable_http"}`
is **rejected before it takes a concurrency slot** — the run ends `error` with a
reason naming the split and pointing at the warm path. It never pauses, never
auto-allows. (Enforced pre-flight in `harness_api/_runner_exec.py` (the run-exec path
rejects OH/Codex `durable_http` before the slot); `_wire_durable_eject` in
`harness_api/_runner_hitl.py` also raises as defense-in-depth. Escape hatch: an app that supplies its own
`permissions.handler_instance` still owns pause/resume.)

## The runnable cells

| Cell | Path | Bar |
|---|---|---|
| **Claude built-in** | native defer → exact-inject, continuation resume | **HARD** (cycle + allow/deny outcome, multi-tool) |
| **Claude custom** | native defer (matcher covers custom) → exact-inject | **HARD** (cycle + outcome, multi-tool) |
| **OpenHarness built-in/custom** | — | **REJECTED** (fail-closed; use warm hold) |
| **Codex built-in** | — | **REJECTED** (fail-closed; use warm hold) |
| **Codex custom** | — | **N/A** (ungated by design) + rejected |

## How to test

### Hermetic (no model, provider-agnostic — the transport logic)

```bash
uv run --no-sync python -m pytest \
  warden/tests/harness_api/test_runs_durable_hitl.py -q
```
Covers the transport state machine (pause / slot-release / allow / deny / idempotent
/ SLA / restart-survival / no-secret / no-regress / route), **multi-tool convergence**
(the regression that caught the resume-prompt bug: a 3-tool mock that restarts iff
re-sent the original prompt — passes only with the continuation resume), and
**fail-closed** (OH/Codex `durable_http` → run ends `error`, no pause, no slot).

### Live — the full bed gate (Docker, isolated single credential)

```bash
cd warden
./docker/run.sh --m6-hitl        # claude HARD: builtin+custom × allow,deny, multi-tool convergent
./docker/run.sh --m6-hitl-oh     # openharness: asserts the fail-closed rejection (no Ollama needed)
./docker/run.sh --m6-hitl-codex  # codex: asserts the fail-closed rejection (no cred needed)
# widen Claude: M6_CASES=allow,deny,idempotent,sla M6_TOOLS=builtin,custom ./docker/run.sh --m6-hitl
```
Gate driver: [`tests/e2e/m6_hitl_smoke.py`](../../../tests/e2e/m6_hitl_smoke.py) —
`run_case` drives `_confirm_loop` (confirm **every** ask until terminal, so multi-tool
Claude converges); Claude cells assert cycle+outcome, OH/Codex cells assert rejection.
Cost: OAuth Claude only — **never the API-key lane**; OH/Codex drive no model
(rejected pre-flight).

> **Don't run the Claude cell from *inside* a Claude Code session** — the nested
> `claude` binary conflicts (`Error in hook callback hook_0: Tool permission stream
> closed`). Use the Docker bed or a plain terminal.

### Live — the host probe (single cell, no Docker)

```bash
# Claude — OAuth from Keychain, API key stripped (exact-id, HARD)
OAUTH="$(security find-generic-password -s 'Claude Code-credentials' -w \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])')"
env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$OAUTH" PYTHONPATH=. uv run --no-sync python \
  warden/scripts/runs-api-hitl-probe.py --provider claude --tool builtin --case allow --base /tmp/m6live
```

## Verified status

**Hermetic (2026-07-24):** all green — `test_runs_durable_hitl.py` (16 cases incl.
multi-tool convergence + decision-aware deny + fail-closed) + the full harness suite.

**Live:**

<!-- LIVE_RESULTS -->
**Claude bed gate — `./docker/run.sh --m6-hitl` (OAuth, 2026-07-24): PASS (all 4 cells).**

| Cell | Result | Confirms | Notes |
|---|---|---|---|
| **Claude** builtin/allow | ✅ PASS | 1–9 | exact-id, tool ran, converged |
| **Claude** custom/allow | ✅ PASS | 4–6 | **multi-tool convergence proven live** — several sequential pauses, each exact-id confirmed |
| **Claude** builtin/deny | ✅ PASS | 5 | deny-aware continuation → model stops retrying → run completes, tool not run |
| **Claude** custom/deny | ✅ PASS | 1 | denied, model reports it cannot proceed |
| **OpenHarness** builtin durable_http | ✅ PASS (fail-closed) | — | rejected pre-flight; `error` names the split; no Ollama contacted |
| **Codex** builtin durable_http | ✅ fail-closed (same code path) | — | rejected pre-flight; covered by hermetic + identical wiring |

> **Two bugs the live bed caught that the hermetic mocks could not:**
> 1. **Multi-tool storm (allow)** — pre-fix, each resume restated the task, so a
>    multi-tool agent restarted its plan (12+ confirms, never converged). Fixed by the
>    neutral-continuation resume. custom/allow now converges in 4–6 confirms.
> 2. **Deny storm (builtin/deny)** — a first-cut allow-framed continuation told the
>    model to "proceed", so on a "must-finish" task (Write "do it now") it re-issued the
>    *denied* call every resume (fresh id, no stored decision → re-eject → re-pause),
>    stalling at the confirm cap. Fixed by a **decision-aware** continuation: a deny
>    resumes with "the action was denied, do NOT retry" so the model gives up and the
>    run terminates. builtin/deny now converges in ~5 confirms.
>
> Both are the class of gap the mock could not surface (single-tool, never-retries).
> This is why the live bed run is the real proof, not the hermetic suite alone.

## Related

- [hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md) — the in-process
  defer mechanic this transport wraps (warm hold vs durable eject); **the HITL path
  for OpenHarness/Codex**.
- [tool-permission-gating.md](./tool-permission-gating.md) — the gate every tool flows through.
- [provider-permission-behavior.md](./provider-permission-behavior.md) — the six-dimension conceptual map.
- 07-durable-hitl.md — the M6 plan + §7 what-was-built.
- 07b-durable-hitl-provider-split.md — the split this guide reflects.
