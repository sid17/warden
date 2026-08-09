# Durable HTTP HITL — how to run the tests & current status

**What this is.** The runbook for testing **durable human-in-the-loop over HTTP**
(M6 / 07b): the Runs-API `POST /runs` → `requires_action` → `POST
/runs/{id}/tool_confirmation` pause/resume cycle. It tells you the three ways to run
it (cheapest → strongest), the exact commands, and **where it stands today**.

> How the mechanism works (not repeated here):
> [durable-hitl-over-http.md](./durable-hitl-over-http.md). One-line version: only
> **Claude** can do it (native `defer` resumes the exact deferred `tool_use_id` by
> *continuing* the conversation); **OpenHarness/Codex are hard fail-closed** on this
> path and use the in-process warm hold instead.

---

## Current status (2026-07-24)

| Provider · cell | Result | Verified at | How |
|---|---|---|---|
| **Claude** builtin/allow | ✅ PASS | **live Docker bed** | exact-id, tool ran |
| **Claude** custom/allow | ✅ PASS | **live Docker bed** | multi-tool convergence (4–6 sequential pauses) |
| **Claude** builtin/deny | ✅ PASS | **live Docker bed** | decision-aware deny → model stops, run completes |
| **Claude** custom/deny | ✅ PASS | **live Docker bed** | denied, model reports it can't proceed |
| **OpenHarness** durable_http | ✅ fail-closed (rejected) | **live host** | rejected pre-flight, no Ollama contacted |
| **Codex** durable_http | ✅ fail-closed (rejected) | hermetic only | identical code path to OH; **bed run is the one open cell** |

- **Hermetic:** `tests/harness_api/test_runs_durable_hitl.py` — **16 cases**, all green
  (pause / slot-release / allow / deny / idempotent / SLA / restart-survival /
  no-secret / no-regress / route + **multi-tool convergence** + **decision-aware deny**
  + **fail-closed OH/Codex**). Full harness suite green (1206 passed, 2 skipped).
- **Claude bed gate** `./docker/run.sh --m6-hitl`: **PASS — all 4 cells** (builtin/custom
  × allow/deny).
- **Bottom line:** Claude durable HTTP HITL is **done and live-proven**; OH/Codex are
  **correctly refused** on this path. The only cell not yet re-run live in the bed is
  Codex fail-closed (proven hermetically; command below closes it).

---

## Before you run

- **Credentials:** Claude OAuth in the macOS Keychain (`claude setup-token` creates it).
  Docker running. OH/Codex durable cells need **no** model/credential — they reject
  pre-flight.
- **Cost golden rule:** OAuth Claude + free Ollama only — **never the API-key lane**
  `env -u ANTHROPIC_API_KEY` / `env -u OPENAI_API_KEY` keep you off it.
- **Don't run the Claude cells from *inside* a Claude Code session** — the nested
  `claude` binary conflicts (`Error in hook callback hook_0: Tool permission stream
  closed`). Use the Docker bed (isolated) or a plain terminal.
- Safe preflight recon: check credentials are present (booleans only, no secrets) before running.

---

## Tier 1 — Hermetic (free, seconds, no creds, no Docker)

The inner loop. A mocked provider drives the **real Runner** through the actual routes,
so it proves the transport state machine. Run constantly.

```bash
cd warden
uv run --no-sync python -m pytest tests/harness_api/test_runs_durable_hitl.py -q
```

What it can't prove: real-model behaviour (multi-tool sequencing, retry-after-deny) —
that needs a live tier. Two real bugs slipped past hermetic and were only caught live
(see "What the live bed caught" below).

## Tier 2 — Live host probe (one cell, no Docker)

Drives the **real provider subprocess** through the routes in-process (ASGI). Good for
iterating on a single cell.

```bash
cd warden
# Claude — OAuth from Keychain, API key stripped
OAUTH="$(security find-generic-password -s 'Claude Code-credentials' -w \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])')"
env -u ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN="$OAUTH" PYTHONPATH=. uv run --no-sync python \
  warden/scripts/runs-api-hitl-probe.py --provider claude --tool builtin --case allow --base /tmp/m6live

# OpenHarness / Codex — expect the fail-closed rejection (PASS (fail-closed))
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. uv run --no-sync python \
  warden/scripts/runs-api-hitl-probe.py --provider openharness --tool builtin --case allow --base /tmp/m6live
```

`--tool {builtin,custom} --case {allow,deny,idempotent,sla}`. Verdict per cell: `PASS`,
`PASS (fail-closed)` (OH/Codex), or a `FAIL`/`PARTIAL` with the trace.

## Tier 3 — Live Docker bed gate (the strong proof)

The real provider in the isolated image, one injected credential. **This is the gate
that counts** — `docker build` runs first (cached after ~2–5 min).

```bash
cd warden
./docker/run.sh --m6-hitl                               # Claude HARD — builtin+custom × allow,deny
./docker/run.sh --m6-hitl-oh                            # OpenHarness — asserts fail-closed (no Ollama)
env -u OPENAI_API_KEY ./docker/run.sh --m6-hitl-codex   # Codex — asserts fail-closed (no cred)

# widen the Claude matrix:
M6_CASES=allow,deny,idempotent,sla M6_TOOLS=builtin,custom ./docker/run.sh --m6-hitl
```

- **Claude** is strict: each cell asserts the pause→confirm→resume cycle **and** the
  allow/deny outcome, multi-tool convergent. A green run ends `M6 DURABLE-HITL GATE:
  PASS (claude)`.
- **OpenHarness/Codex** cells assert the run is **rejected** (ends `error` naming the
  split) — they drive no model, so they need no credential and are cheap.
- Knobs: `M6_CASES` (default `allow,deny`), `M6_TOOLS` (default `builtin,custom`),
  `M6_TIMEOUT_S` (default 420). Driver: `tests/e2e/m6_hitl_smoke.py`. Creds: OAuth
  only (never a billed key).

---

## Reading the output

- **`confirms=N after-loop=succeeded`** — the run converged after N confirmations
  (bounded ≈ number of tool calls). Multi-tool cells legitimately take several.
- **`after-loop=requires_action`** (with `confirms` at the cap ~14) — a **non-converging
  storm**: the run never terminated. This is a FAIL; investigate the resume prompt.
- **`Error in hook callback hook_0: … Tool permission stream closed`** — noisy SDK
  teardown log when a subprocess stdin closes with a pending control request. On its own
  it does **not** mean the cell failed — judge by the `PASS`/`FAIL` verdict line, not the
  stack. (If it appears with a stuck `requires_action`, that's the real signal.)

## What the live bed caught (and hermetic couldn't)

Two real bugs surfaced only at Tier 3 — the reason the bed run is mandatory, not
optional:

1. **Multi-tool allow storm** — the first durable resume re-sent the *original prompt*,
   so a multi-tool agent restarted its plan every resume (never converged). The
   single-tool mock never issued a 2nd tool. Fixed: neutral continuation on resume.
2. **Deny storm (builtin/deny)** — an allow-framed continuation told the model to
   "proceed", so on a "must-finish" task it re-issued the *denied* call every resume
   (re-eject → re-pause), stalling at the cap. The deny mock *reported blocked and
   stopped* instead of retrying. Fixed: **decision-aware** deny continuation ("denied,
   do not retry").

Both fixes now have hermetic regression tests, but they were **found live**.

## Related

- [durable-hitl-over-http.md](./durable-hitl-over-http.md) — how the mechanism works + the full live-results table.
- [hitl-defer-warm-vs-durable.md](./hitl-defer-warm-vs-durable.md) — the in-process warm hold (the OH/Codex HITL path).
- `07b-durable-hitl-provider-split.md` — the plan/decision behind the Claude-only split.
