# 08 — The Session Contract & End-to-End Testing

> **What this is.** The normative answer to: **(A) what the harness guarantees about a *session*** — identity,
> lifecycle, resume-with-memory, durability, isolation — and **(B) how we prove it**, first with hermetic unit
> tests and then in the Docker bed under real (OAuth / free-Ollama) auth. It is the session-level companion to
> [`07-provider-contract-and-testing.md`](./07-provider-contract-and-testing.md): `07` governs a single
> provider turn; `08` governs a conversation that spans turns, closes, resumes, and survives a restart.
>
> **Scope of this doc:** the three finalized providers — Claude SDK (`ClaudeSession`), OpenHarness
> (`OpenHarnessSession`), Codex SDK (`CodexSdkSession`). Working scope + TODO ledger:
> the session-management SCOPE build note (internal).

---

## Part A — What a session IS

A **session** is one continuing conversation with an agent, identified by a provider-owned id and backed by a
durable index row + an on-disk transcript. The harness owns the session's *lifecycle and durability*; the
provider SDK owns the *conversation state* (the model's memory of prior turns).

### A.1 The session contract (what MUST hold)

| # | Guarantee | Grounded in |
|---|---|---|
| **S1 Identity** | A session has a provider-owned id (Claude `session_id`, Codex `thread_id`, OpenHarness uuid), **captured from the first streamed message**, stable across resume. | C4 / SESS-2 |
| **S2 Registration** | After id capture, the session is registered in the durable index (`provider`, `workspace_path`, `jsonl_path`, `status`). | SESS-2 / AUD-2 |
| **S3 Lifecycle** | `create → active → close` (archived). `close()` tears down the provider + archives the row; the id remains resumable. | C4 |
| **S4 Resume = re-attach + MEMORY** | Resuming a session id re-creates the provider with `resume_session_id` **and the model regains access to prior turns** — a resumed session can answer questions about earlier messages. Re-attach without memory does **not** satisfy S4. | C4 / SESS-2 |
| **S5 Provider-match** | Resuming a session under a *different* provider than it was created with is rejected (create-fresh, never cross-attach). | SESS-2 |
| **S6 Durability** | The index is on-disk (survives process restart). With **persistence active** (`task_id` set), the task workspace + transcript are snapshotted after each turn, so a fresh process can resume. | SESS-3 (orchestrator) |
| **S7 Isolation** | Concurrent sessions do not bleed conversation history or credentials (ties to C2/T3 per-instance isolation). | C2 |
| **S8 Transcript addressable** *(forward-looking)* | Each session's transcript is discoverable via `jsonl_path`. Consumed by a future history/audit feature; not required for S4 recall. | OBS-2 / AUD-1 |

**Out of session scope** (same carve-outs as the provider contract): output filtering (SAFE-1) and the
snapshot mechanism internals (SESS-3) are orchestrator/persistence concerns; a provider only re-attaches.

### A.2 The lifecycle, end to end

```
create(provider, repo) ─▶ start() ─▶ [first msg] capture session_id ─▶ register(index)
      ▲                                                                      │
      │  resolve_turn_session 3-way lookup per turn                          ▼
      │  (client-active ▸ orchestrator-current ▸ DB-resume ▸ create)      send(prompt)*  ─▶ snapshot (if persist)
      │                                                                      │
   resume(id) ◀──────────────── close(id) [archive] ◀───────────────────────┘
```

The per-turn resolver (`orchestrator/stream_runtime.py:resolve_turn_session`) is the heart: a turn either
reuses a live session, resumes one from the index (the **crash-recovery path** — in-memory map empty, SQLite
persists), or creates fresh. **The type-guard in Step 1 must name the real provider class** (see §D bug 4a).

### A.3 What "resume" restores — the per-provider tier map

The bar (S4) is **model memory**. How each provider meets it:

| | Claude SDK | Codex SDK | OpenHarness |
|---|---|---|---|
| Re-attach mechanism | `options.resume = id` | `thread_resume(thread_id)` | rebuild engine, pin id |
| History source on resume | SDK reloads `…/projects/*.jsonl` | SDK thread state (`.codex`) | `load_messages()` from `sessions/*.jsonl` |
| Meets S4 (memory) today | ✅ | ✅ (verified — recall gate; Q2 answered) | ✅ (B-OH fixed — seeds from transcript on resume) |
| `jsonl_path` populated | ✅ `discover_jsonl_path` | ✅ `discover_jsonl_path` (B-jsonl fixed) | ✅ set from transcript |

**All three providers now meet the full contract**, proven by the resume-recall bed gate (§C.2). Codex's
orchestrator event-drop (§D 4b) and OpenHarness's cold resume (§D B-OH) are FIXED; each is annotated below with
its commit. This doc's test suite is what forced all three to parity.

---

## Part B — The test strategy (two tiers, no API-key lane)

Mirrors `07`'s bed philosophy: **hermetic code-truth first, real-auth fidelity second.**

| Tier | Runs | Faithful because | Cost |
|---|---|---|---|
| **1 — Hermetic (unit)** | always, CI-safe | fake providers **named after the real classes** exercise the resolver/index/lifecycle honestly | $0 |
| **2 — Docker bed (real auth)** | local / opt-in, skip-in-CI | clean container, one injected credential; a real model actually recalls a planted fact | **Claude OAuth = $0 marginal** (subscription); **Codex OAuth**; **OpenHarness = free Ollama `qwen3:8b`** |

**Auth/cost rules:** use **OAuth** for Claude (already paid, no per-token bill) and Codex (`auth.json`); use
**free local Ollama** for OpenHarness. **Never the API-key lane** — it bills separately. If Ollama recall is
too weak to demonstrate memory, point OpenHarness at a small cheap cloud model *for the recall gate only*.

### B.1 The resume-recall gate (the definition of "it works")

The canonical S4 proof, per provider:

```
turn 1: "Remember this: my favorite color is heliotrope."   ─▶ assert acknowledged
close(session_id)
resume(session_id)          # same id, new provider instance
turn 2: "What is my favorite color?"                        ─▶ assert "heliotrope" in reply
```

If turn 2 recalls the planted fact, the harness resumed the session **and** the model reloaded history — S1–S4
proven in one gate. This is the test the user asked for ("ask it what was in the last session").

---

## Part C — The gate catalog

### C.1 Tier-1 hermetic (unit) — `tests/core/`, `tests/harness_api/`

| Gate | Contract | Method | Providers |
|---|---|---|---|
| create → register → capture id → DB row | S1/S2 | fake provider yields id on first msg; assert index row | all 3 (fakes named `ClaudeSession`/`CodexSdkSession`/`OpenHarnessSession`) |
| resume-after-close reuses id | S3/S4(mech) | close then resolve → resume path; assert id pinned | all 3 |
| switch → come back (both live) | S3 | two sessions in `_sessions`; resolve each by id; assert no re-resume | all 3 (**catches 4a**) |
| DB-resume (fresh manager) | S6 | new manager, empty `_sessions`, shared SQLite; resolve → resume | all 3 |
| provider-mismatch rejected | S5 | resume codex id as claude → create-fresh | codex-name |
| `jsonl_path` round-trips non-null | S8 | register → read back; assert non-null | all 3 (contingent B-jsonl) |
| send emits ≥1 content event + terminal | 07-C4 via orchestrator | drive `send` through orchestrator; assert events | all 3 (**catches 4b**) |

### C.2 Tier-2 Docker bed (real auth) — `docker/run.sh --*-session`

| Gate | Contract | Bed command | Pass criterion | Status |
|---|---|---|---|---|
| Claude resume-recall | S1–S4 | `./docker/run.sh --claude-session` (OAuth) | turn-2 recalls the planted fact | ✅ **PASS** (2026-07-18) |
| Codex resume-recall | S1–S4 | `./docker/run.sh --codex-session` (OAuth) | turn-2 recalls the planted fact | ✅ **PASS** (2026-07-18, Q2 answered) |
| OpenHarness resume-recall | S1–S4 | `./docker/run.sh --openharness-session` (Ollama `qwen3:8b`) | turn-2 recalls the planted fact | ✅ **PASS** (2026-07-18, Q3 answered) |
| Crash-recovery (persistence) | S6 | `./docker/run.sh --claude-crash` / `--codex-crash` / `--openharness-crash` (persistence-active snapshot → **wipe** task dir → restore → resume → recall) | recall survives a wiped+restored task dir | ✅ **PASS** ×3 (2026-07-18, Q4/Q5 answered) |

Each new bed mode follows `07` Part E (driver under `tests/e2e/`, `COPY` into the Dockerfile, a mode in
`entrypoint.sh`, host wiring in `run.sh`). The clean-container invariant still holds: only the injected
credential is present, so a completed recall already proves auth + session load. The three resume-recall gates
share one driver (`tests/e2e/_session_recall.py`) that plants a fact → closes → resumes the SAME id in a
**fresh** `SessionManager` → recalls, all through the real `Orchestrator` (so the codex gate also exercises the
4b handler path). The persistence-active **crash-recovery** variant is built and green for all three providers —
see **Part F**.

---

## Part D — Known issues this contract exposed (status)

- **4b [P1] — Codex through the Orchestrator emits zero events. ✅ RESOLVED (commit `f653dd8b`).**
  `get_message_handler("codex")` ran already-normalized dicts through `transform_codex_message` (keys on absent
  `"type"`) → dropped everything. Fixed with a named passthrough `transform_codex_sdk_message` (guards non-dict);
  the legacy exec handler stays in place (mv-only). `orchestrator/stream_runtime.py` (`get_message_handler`),
  `providers/codex/sdk_message_handler.py`. Proven by `tests/core/test_stream_runtime_codex.py` (≥1 text
  `MessageEvent` through the real orchestrator; was 0) and by the `--codex-session` recall gate.
- **4a — stale session-type map. ✅ RESOLVED (commit `f653dd8b`; hardened since).** The original bug: a
  class-name → provider map (then `PROVIDER_SESSION_TYPES`) mapped `"codex"` to the retired `"CodexSession"`
  while the live class was `"CodexSdkSession"` → Step-1 reuse missed → wrong re-resume on switch-and-come-back.
  The name-based map is **gone**: reuse now matches each live session's stable `PROVIDER` attribute via
  `_session_provider_key()` (`orchestrator/stream_runtime.py`, the `_session_provider_key` function, consumed in
  `resolve_turn_session`), so a class rename can no longer silently break Step-1 reuse (the N9 hardening).
  Proven by `test_session_resume.py::test_switch_and_come_back_reuses_live[codex]` (fail-before/pass-after).
- **B-OH — OpenHarness cold resume. ✅ RESOLVED (commit `7625233f`).** `resume_session_id` stored but never
  consumed; `start()` built a fresh engine. Fixed: `_load_history()` parses the transcript on resume and calls
  `engine.load_messages()` (fail-soft per LAW 4). `providers/openharness/session.py`. Proven by
  `tests/providers/test_openharness_session_resume.py` and the `--openharness-session` recall gate (`qwen3:8b`).
- **B-jsonl — Codex `jsonl_path` never set** (S8). ✅ **RESOLVED (commit after `7625233f`).** Added
  `CodexSdkSession.discover_jsonl_path()` — exact thread-id match under the resolved codex home (pinned
  `_codex_home`, else ambient `CODEX_HOME`, else `~/.codex`), no most-recent fallback. All three finalized
  providers now populate `jsonl_path`; verified in the bed (claude `…/projects/*.jsonl`, codex
  `$CODEX_HOME/sessions/**/rollout-*-<tid>.jsonl`, openharness `…/sessions/<sid>.jsonl`).
  `providers/codex/sdk_session.py`, `tests/providers/test_codex_sdk_jsonl_path.py`.

## Part E — Known limitations (documented, not bugs)

- **Persistence is off without a `task_id`** — then a real crash loses the whole workspace (only the SQLite row
  survives), so the provider cannot find its transcript. Cross-process crash-recovery **requires** persistence
  active (`orchestrator/orchestrator.py`, the `_persist_active = bool(persist_cfg and task_id)` gate); the
crash-recovery gates drive it, see **Part F**.
- **No web/WS session API** — list/resume are the in-process `ChatAPI` + CLI; run execution is the Runs API
  (`harness_api/app.py`). A history-replay endpoint does not exist and is out of scope.
- **`describe_auth` / AUTH-3** — declared seam, no consumer; deferred (see `07` A.4).

## Part F — Crash-recovery (S6): what it proves, how it's built, how to extend it

Resume-recall (§C.2, S1–S4) proves memory survives a fresh `SessionManager` **with the transcript still on
disk**. Crash-recovery proves the stronger guarantee (**S6 durability**): memory survives when the **entire
task workspace is destroyed** and rebuilt from the after-turn snapshot — the real "the node died / the next
turn runs in a brand-new stateless container" path.

### F.1 The mental model (read this first)

Two things live in different places, on purpose:

- **The session transcript** (the model's memory) lives **inside the task workspace**, under a per-provider home
  the orchestrator pins there: `<task>/.claude-home`, `<task>/.codex`, `<task>/.openharness`
  (`provider_home_kwargs`). Because it is inside the task dir, it is captured by the **after-turn snapshot**.
- **The session index** (SQLite: the id→provider→workspace row) lives **outside** the task workspace. It is the
  durable *pointer* a crash keeps even when the workspace is gone.

So crash-recovery = *"the snapshot carried the transcript across a wiped workspace, and the index pointer let us
find it again."* If either is missing, resume degrades to id-reattach with **no memory**. The whole design turns
on one rule: **the transcript must be inside the snapshotted task dir.** (This is why the OpenHarness
home-pinning fix mattered — it wrote to the global `~/.openharness`, *outside* the snapshot, so a wipe lost it.)

### F.2 The gate (as built)

`./docker/run.sh --claude-crash | --codex-crash | --openharness-crash` — one container, persistence **active**
(`task_id` set), driver `tests/e2e/_session_crash.py`:

```
turn 1  plant "heliotrope"      → orchestrator snapshots <base>/<user>/<task> to a local tarball
rm -rf  <base>/<user>/<task>    → the ONLY surviving copy of the transcript is now the tarball
turn 2  resume (fresh manager)  → ensure_restored rebuilds the task dir from the tarball
        "what's my color?"      → assert "heliotrope"   (recall ⇒ memory crossed the wipe)
```

All three are **GREEN** (Claude OAuth, Codex OAuth, OpenHarness Ollama `qwen3:8b`), each printing
`WIPED … exists now: False` → `restored … True` → turn-2 recall. Provider notes:

- **Claude** — cleanest: auth is an env-var token (survives), transcript in `<task>/.claude-home`.
- **Codex** — its SDK couples **auth + transcript** in one `CODEX_HOME`. Under persistence that home is pinned
  to `<task>/.codex` (so the rollout is snapshotted), which has no `auth.json`. **A1/A2 (exclude + re-inject):**
  the snapshot **excludes** `auth.json` (`persistence/archive.py` `_is_credential`) so the OAuth token never
  travels in the tarball, and the **production restore path** (`prepare_persisted_turn` →
  `workspace/credentials.py reinject_credentials`) re-hydrates `<task>/.codex/auth.json` out-of-band from the
  mounted ambient credential on every persisted turn — turn 1 and post-wipe restore alike. Safe for a **remote
  (S3) backend**: the object carries the transcript, never the credential. See the
  ADR-credential-backup-separation build note (internal).
- **OpenHarness** — no cloud auth (Ollama). Its transcript at `<task>/.openharness` is read back on resume by
  the B-OH `_load_history()` seam after restore.

**Robustness bugs this gate surfaced and fixed** (`persistence/archive.py`, commit `148a53ce`) — real
production hazards, not test artifacts: (1) codex extracts arg0 helper binaries as **absolute-target symlinks**
under `.codex/tmp`, which Python 3.12+'s hardened `data` tar filter refuses on restore → snapshot now drops
escaping links at archive time and extracts with an explicit `filter="data"`; (2) codex git-clone scratch
churns **lockfiles that vanish between the walk and the tar `lstat`** → the snapshot now walks manually and
skips entries that disappear mid-walk instead of aborting the whole backup. Resume-critical files (transcript,
`auth.json`) are regular files and travel unchanged.

### F.3 How to think about crash-recovery testing in the future

When you add a provider, a backend, or a persistence change, re-derive the gate from these invariants rather
than copying the script blindly:

1. **Locate the transcript. Is it inside the task dir?** New provider → add its home to `provider_home_kwargs`
   so its transcript lands under `<task>/`. If it isn't in the snapshot, crash-recovery *cannot* work — no test
   will save you. This is invariant #1.
2. **Separate the credential from the workspace.** If auth is an **env var / token** (Claude, Ollama-none),
   you're clean. If auth is a **file in the provider home** (Codex), the crash test must make it available on
   the restored side — and if the backend is remote, it must **not** be snapshotted in the clear. State which
   case you're in.
3. **Confirm the index is durable and external.** The session row must live outside the wiped workspace (its
   own SQLite path / volume). If a design ever puts the index inside the task dir, a wipe orphans every session.
4. **Make the wipe real.** The test's credibility is the `rm -rf`. Assert `exists() is False` after it and
   `True` after restore. A gate that resumes off an un-wiped disk proves S4, not S6 — don't let it masquerade.
5. **Choose the crash topology deliberately.** *Single container + `rm -rf` between turns* (what we ship) proves
   the snapshot/restore mechanism and is cheap. *Two containers sharing a snapshot-backend + index volume* (the
   worker CLI supports this out of the box: `--task-id --user-id --session-db --storage-backend local --resume`)
   proves a genuinely separate OS process on possibly-different hardware. Reach for the two-container form when
   you need to prove cross-host recovery (e.g. before shipping stateless workers), not for routine CI.
6. **Exercise the backend you actually run.** The gate uses the **local** tar backend (free, no cloud). Before
   trusting S3/remote, run the same flow against it once — remote adds credential-in-tarball and
   eventual-consistency concerns the local backend hides.
7. **Expect provider scratch to fight you.** Agents write ephemeral, racy, symlink-y junk in their home
   (`.codex/tmp`, git clones, lockfiles). The archive layer must be race-safe and symlink-safe (F.2). When a
   *new* provider's snapshot flakes, suspect its scratch first, and prune/skip it rather than widening filters.
8. **Keep it hermetic where you can.** The archive-layer robustness (symlink/vanish handling) is unit-tested in
   `tests/persistence/` without any model — push provider-independent guarantees down to that tier; reserve the
   Docker gate for the real model-recall proof.

The one-line test of whether a future change is safe: **can a brand-new process, given only the durable index
row and the snapshot backend, resume the session and have the model recall a fact from before the crash?** If
yes, S6 holds.

## Provenance

Written 2026-07-18 after the provider-finalization review, from two session-management mapping passes (in-proc
lifecycle + frontend/durability). Grounded in the capability-contracts build notes (internal, C4/SESS-*),
`orchestrator/{stream_runtime,session/*}.py`, the three provider `session.py` files, and the persistence/
workspace layer. Working scope + decisions: the session-management SCOPE build note (internal).
