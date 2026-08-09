# 07 — The Provider Contract & End-to-End Docker Testing

> **What this is.** The normative answer to two questions every new provider raises: **(A) what must a
> provider provide** (the typed contract + capability flags + the mechanics it must implement), and **(B) how
> do we prove it, feature by feature, in the clean Docker bed**. It sits next to `01`–`06` because the
> provider is the unit the capability contract is written against; this doc is the *implementer's + tester's*
> companion to the capability-contracts build note (internal) (C1–C9, the
> acceptance layer) and [`01-conceptual-model.md`](./01-conceptual-model.md) §5/§9/§10 (the design).
>
> **Use it as a checklist** when adding a provider (Phase 2 OpenHarness, Phase 3 Codex Python SDK, …): the
> contract in Part A/B is what to build; the bed methodology in Part C–F is how to gate it. **Claude SDK is
> the reference implementation** — see [`providers/claude-sdk.md`](./providers/claude-sdk.md) for a worked
> example of every property below.

---

## Part A — What a provider IS

A provider is a **pure transport/mechanism** around one agent SDK/CLI. It owns a session's lifecycle, injects
one resolved credential per run, connects the permission decision to the tool loop, delivers custom tools,
surfaces a normalized event stream, and **declares its own capability tier** so the layers above dispatch on
a stated fact, never an `isinstance` check or a runtime surprise. It does **not** know about users, tiers,
budgets, dollars, or seconds (those are the Governor's — `06`), and it does **not** filter output or snapshot
sessions (those are the orchestrator's — `01` §6/§7b). See `schemas/providers.py`.

### A.1 The Protocol (lifecycle — unchanged across all providers)

```python
class AgentProvider(Protocol):
    session_id: str            # captured from the first streamed message, NOT pre-assigned
    jsonl_path: str | None     # per-run transcript path (for history replay)
    async def start() -> None                                  # init SDK/subprocess, connect
    async def send(prompt: str) -> AsyncGenerator[Any, None]   # stream provider-native events
    async def stop() -> None                                   # abort the current turn (tiered)
    async def close() -> None                                  # tear down
    # + the seven capability-flag properties (A.2)
```

**Explicitly OUT of provider scope** (state this in every provider's docstring):
- **SESS-3** snapshot-survives-failed-turn → orchestrator/persistence.
- **SAFE-1** output filtering → a first-class core pass on the drain side of the per-turn queue.

### A.2 The seven capability flags (declared per provider; the Governor/compaction dispatch on these)

Each is a stated fact about what the substrate physically allows. A provider **passes a tiered contract at
its declared tier** (PROV-3) — a "no" here is a *declared rejection*, not a bug.

| Flag | Type | Meaning | Who reads it |
|---|---|---|---|
| `crash_isolated` | `bool` | a child crash is contained (host survives) vs takes down the process | orchestrator blast-radius (PROV-4) |
| `hard_kill_tier` | `os \| cooperative \| none` | can the harness `SIGKILL` the run (`os`), only ask the SDK to stop (`cooperative`), or neither (`none`) | Governor interrupt (C6) |
| `cost_visibility` | `mid_turn \| coarse \| terminal` | when usage/cost becomes visible: streaming, mid-run totals, or only at the end | Governor cost cap (C7), OBS |
| `compaction` | `native \| harness_driven` | provider auto-compacts vs the harness must summarize-then-resume | compaction driver (C9) |
| `supports_hard_deadline` | `bool` | can a wall-clock deadline *truly* stop mid-turn (needs a harness-owned PID) | Governor (C6/GOV-3) |
| `custom_tool_delivery` | `in_proc_list \| mcp \| none` | how custom tools reach the model; `none` ⇒ must **error**, never drop (TOOL-1) | factory + custom-tool wiring |
| `perm_tier` | `arg_level \| name_level \| none` | granularity of the permission decision the provider can enforce | permission chain (C3/PERM-1) |

> **Do NOT add an 8th flag** for sub-agent span nesting — that is a per-provider *wiring* obligation gated by
> the OTEL parity test (T9), not a declared capability (design-coverage §5.3).

### A.3 The typed inputs (explicit `__init__` params — never bare `**kwargs`)

The root cause of nearly every historical gap (N2/N3/B8/B15/C1) was capabilities passed as loose `**kwargs`
and **silently dropped**. The contract's structural fix: every capability a provider can receive is an
**explicit typed param**, and anything left over is a hard `TypeError`.

Standard inputs threaded by the factory/orchestrator: `repo_path`, `can_use_tool`, `model`,
`resume_session_id`, `session_id`, `disallowed_tools`, `system_prompt`, `auth_env`, `custom_tools`, **plus a
per-provider slice** (Claude: `claude_config_dir`; Codex: `codex_home`, `approval_mode`; OpenHarness:
`base_url`, `api_key`, `num_predict`). Accept-and-store even where a tier can't use it yet (that closes the
drop); if a capability genuinely can't be honored, **raise**, don't ignore.

Config lives in the existing nested `HarnessConfig` (`config/models.py`) — `AuthConfig.auth_env`,
`CustomToolsConfig.tools`, `PermissionsConfig.denied_tools`, `AuditConfig.enabled`. Do **not** invent a
parallel dataclass; thread these to the leaf via the factory. Per-run/per-turn values (`can_use_tool`,
`auth_env`, per-turn `tool_scope`) are **runtime** inputs, not declarative config.

### A.4 The `BaseProvider` mixin (shared mechanism — every provider subclasses it)

`providers/base_provider.py` supplies the discipline + shared helpers so providers only override the
genuinely-divergent seams:

- **`_reject_unknown_kwargs(kwargs)`** — the standing guardrail. Every `__init__` calls it last; leftover
  kwargs ⇒ `TypeError`. This is what keeps the silent-drop class closed.
- **`apply_auth_env(env, provider_key, auth_env)`** — the strip-then-inject helper (promoted from the one
  transport that already did per-run auth right). Strips every inherited credential in
  `PROVIDER_AUTH_VARS[provider_key]` from `env`, then overlays `auth_env`. Pure w.r.t. `os.environ`. No-op
  when `auth_env is None` (single-user inherit path unchanged). **Stripping first is load-bearing** — an
  operator's ambient token would otherwise shadow an injected key and concurrent runs would bleed.
- **`describe_auth()`** — the AUTH-3 identity-report seam: report the active identity by **tag/fingerprint,
  never the raw key**. Like `normalize_event`, this is a **declared seam, default no-op today**
  (`base_provider.py:71` returns `{}`; **no provider overrides it yet**). The isolation guarantee is proven
  behaviorally by `--t3`; the reporting wiring lands when the governance/observability phase needs it.
- **`install_hooks(...)`** — one generic seam through which **both** the audit hook (AUD-1) **and** the
  `PreToolUse` sensitive-path hook (SAFE-6) install. Providers with a native hook system implement it.
- **`normalize_event(raw, parent_observation_id=None)`** — the event-normalization seam. Identity
  pass-through today; the concrete `Event` union (`stopped`/`compaction`/`tool_result`) is N1/Phase-4 work.

### A.5 The responsibility surface (grounded in C1–C9)

| Group | The provider must… |
|---|---|
| **Lifecycle** | start / send / stop / close; capture `session_id`; resume by token; pin a session-home (C4) |
| **Auth** | inject `auth_env` at the transport's own point; strip inherited first; both modes; `describe_auth` by tag (C1/C2/AUTH-*) |
| **Permission** | accept `can_use_tool(name, input, ctx)` fail-closed with **args**; honor static `disallowed_tools`; declare `perm_tier`; reduced profiles are a *named* adapter (C3/PERM-*/D6) |
| **Custom tools** | consume-or-**error**, never silently drop; delivery matches reach (TOOL-1/2) |
| **Turn/event** | exactly-one-terminal + session_id; normalized events incl. `stopped/compaction/tool_result` (C4/OBS) |
| **Cost** | per-turn usage + cost on the terminal; mid-turn where the tier allows (C5/C7) |
| **Interrupt** (tiered) | honor wall-clock deadline + cost cap to the degree `hard_kill_tier`/`cost_visibility` allow; declare the tier (C6/C7) |
| **Observability** | baseline OTEL taxonomy under GenAI semconv; nested sub-agent spans; per-run cost on the wire; audit-hook install gated by config (C8/OBS/AUD) |
| **Compaction** (tiered) | compact at split points + emit `compaction`; honor the workflow trigger policy (C9) |
| **Crash isolation** (property) | declare `crash_isolated` + `hard_kill_tier` (PROV-4) |

> **Transcript exposure (OBS-2/AUD-1).** All three providers populate `jsonl_path` via a
> `discover_jsonl_path` scan of the (pinned) session home. The Codex SDK adapter initializes
> `self.jsonl_path = None` (`codex/sdk_session.py`, in `__init__`) and fills it in
> `CodexSdkSession.discover_jsonl_path()` (`codex/sdk_session.py`, `discover_jsonl_path`), which globs the
> thread's on-disk rollout transcript by `session_id` and is fail-soft (LAW 4: log, never crash the turn).

---

## Part B — The per-provider tier map (source of truth for flag values)

"Complete for a cycle" = **C1–C5** (auth both modes + isolation, permission fail-closed arg-level, custom
tools, full turn cycle, cost). **C6–C9 are tiered** — declared now via flags, wired in the later
governance/observability/compaction phases and diffed against the Claude baseline.

| Flag | Claude SDK | Codex Python SDK | OpenHarness |
|---|---|---|---|
| `crash_isolated` | `True` | `True` | `False` |
| `hard_kill_tier` | `cooperative` | `os` | `none` |
| `cost_visibility` | `mid_turn` | `coarse` | `terminal` |
| `compaction` | `native` | `harness_driven` | `harness_driven` |
| `supports_hard_deadline` | `False` | `True` | `False` |
| `custom_tool_delivery` | `in_proc_list` | `mcp` / `none` (instance property) | `in_proc_list` |
| `perm_tier` | `arg_level` | `arg_level` (exec/patch only†) | `arg_level` |

> **Codex `custom_tool_delivery` is an instance property, not a class constant** (`codex/sdk_session.py`, the `custom_tool_delivery` property):
> it returns `mcp` when the ungated opt-in `allow_ungated_custom_tools` is set **and** `custom_tools` are
> present (delivered UNGATED via an in-proc streamable-HTTP MCP server), else `none`. Codex now **does** deliver
> custom tools — the earlier "D6 → must error" claim is retired — but only behind that explicit opt-in;
> **default off still raises** (consume-or-error, never drop, TOOL-1).
>
> **† Codex `perm_tier = arg_level` is honest for the exec/patch surface ONLY.** The arg-level `can_use_tool`
> gate covers `item/commandExecution` + `item/fileChange` approvals. MCP custom tools ride the
> `mcpServer/elicitation/request` accept path and are **NOT** gated by `can_use_tool` — which is exactly why
> their delivery is ungated behind the opt-in. OpenHarness `perm_tier` is now plain `arg_level` (B15 closed —
> `can_use_tool` runs as a `PRE_TOOL_USE` hook that sees the real `tool_input`).

---

## Part C — The Docker test bed: why, and how it's built

**The one principle that makes a green trustworthy:** in a **clean container the only credential present is
the injected one**. There is no ambient key to fall back on, so a completed *"hello"* turn already **proves
auth** — if the reply comes back, the injected credential authenticated. A false green is impossible by
construction. Every feature gate builds on this.

The bed lives in `warden/docker/`:

- **`run.sh`** (host side) — builds the image, extracts exactly one credential (Claude OAuth from the macOS
  Keychain, or `ANTHROPIC_API_KEY` from env; Codex `auth.json` mounted `:ro`), and `docker run`s a single
  mode. **Secrets cross only via `-e`/`-v` at run time; never baked into the image, never printed.**
- **`entrypoint.sh`** (container side) — `strip_all_except <KEEP>` unsets every other credential, then
  `assert_clean_except <KEEP>` **fails the run** if anything else is present (defense in depth), then runs
  the mode's driver. Exit code is the gate.
- **`Dockerfile`** — copies the engine package and **ships the e2e smoke drivers individually** (not the
  whole test tree) so they import as `warden.tests.e2e.*`. The engine is self-contained — no external
  config packages are vendored.

### The three test lanes (cost discipline)

| Lane | Use for | Why faithful | Cost |
|---|---|---|---|
| **Real credential** (fidelity) | auth (T1/T2), real tool-use, permission fidelity | only the injected cred is present | a few cents — trivial prompts, one turn, capped output |
| **Free Ollama** (plumbing) | permission/tool wiring on OpenHarness | permissions are in-process/SDK-level, so free-lane is faithful | $0 (`host.docker.internal:11434`, `qwen3:8b`) |
| **Unit code-truth** | kwargs-reject, strip-first, flag values, consume-or-error | pure code, no model | $0 |

---

## Part D — The gate catalog (one row per capability; reuse per provider)

Each capability maps to a driver + a bed command + a binary pass criterion. Drivers live in
`warden/tests/e2e/` and are exit-code gates runnable as `python -m warden.tests.e2e.<name>`.

| Gate | Contract | Bed command | Pass criterion |
|---|---|---|---|
| **Auth OAuth** | C1/T1 | `./docker/run.sh --auth oauth` | exit 0 — hello turn completes with only the OAuth token present |
| **Auth API-key** | C1/T2 | `./docker/run.sh --auth api-key` | exit 0 — hello turn completes with only the API key present |
| **Per-instance isolation** | C2/T3 | `./docker/run.sh --t3` (also runs local — credential-free) | two sessions/two keys, each `options.env` carries only its own key; inherited `ANTHROPIC_AUTH_TOKEN` stripped |
| **Permission fail-closed** | C3/T4 | `./docker/run.sh --perm-smoke` | callback fired, denied tool's side effect **absent**, allow-control side effect **present**, args seen |
| **Custom tool** | TOOL-1/2 | `./docker/run.sh --custom-tool` | real turn — the registered tool's handler executes (marker side effect) |
| **Full cycle** | C4/T5 | driver: ≥1 msg + exactly one terminal + non-empty `session_id`; resume works | one terminal, resumable |
| **Cost per turn** | C5/T6 | driver reads the terminal usage | per-turn `usage` + `cost` recorded (> 0) |
| **kwargs-reject / strip / flags** | AUTH-2 etc. | `pytest warden/tests/providers/` | unit green |
| **Interrupt / OTEL / compaction** | C6–C9 | T7/T8/T9/T10 — **diffed against the Claude baseline** at the provider's declared tier | later phases |

### The real per-provider gate matrix (every bed mode that exists)

Every (capability × provider) cell has a gate or is explicitly deferred (C6–C9). These are the **actual bed
modes wired into `docker/run.sh` + `docker/entrypoint.sh`** today:

| Gate | Provider | Bed command | What it proves |
|---|---|---|---|
| Auth OAuth | Claude | `./docker/run.sh --auth oauth` | hello turn with only the OAuth token present (C1/T1) |
| Auth API-key | Claude | `./docker/run.sh --auth api-key` | hello turn with only the API key present (C1/T2) |
| Per-instance isolation | Claude | `./docker/run.sh --t3` (also runs local, credential-free) | each `options.env` carries only its own key; inherited `ANTHROPIC_AUTH_TOKEN` stripped (C2/T3) |
| Permission fail-closed | Claude | `./docker/run.sh --perm-smoke` | `can_use_tool` fired, denied side effect absent, allow-control present, args seen (C3/T4) |
| Custom tool | Claude | `./docker/run.sh --custom-tool` | registered in-proc-list tool's handler executes (marker) (TOOL-1/2) |
| Arg/path perm + custom tool | OpenHarness | `./docker/run.sh --openharness-perm` (host Ollama, free) | arg-level deny sees real `{path, content}` + blocks; allow-control writes; custom tool runs (C3/B15) |
| Auth both modes | Codex | `./docker/run.sh --codex-auth` | hello turn — API-key (`OPENAI_API_KEY`) and OAuth (mounted `auth.json`) (C1) |
| Permission fail-closed | Codex | `./docker/run.sh --codex-perm` | exec/patch approval declined → action blocked; allow-control writes (C3/T4) |
| Ungated MCP custom tool | Codex | `./docker/run.sh --codex-custom-tool` | opted-in in-proc HTTP MCP tool's handler runs (marker); default-off raises (TOOL-1) |
| Interrupt / OTEL / compaction | all | — | **deferred C6–C9** (diffed against the Claude baseline in the later phases) |

**The suite is the standing gate:** every provider change re-runs its (provider × capability) rows; a red
cell blocks. The bed's exit code is the gate.

---

## Part E — Adding a new provider's tests (checklist)

1. **Write the driver** under `warden/tests/e2e/<name>.py`, mirroring `t4_perm_smoke.py`: it must be
   self-contained, print a clear PASS/FAIL banner, and `sys.exit(0/1)`. Structural (credential-free) proofs
   are ideal where possible — see `t3_isolation.py` (neutralizes `connect()`, inspects the resolved env).
2. **Ship it in the image** — add a `COPY tests/e2e/<name>.py …` line to `docker/Dockerfile` (drivers are
   copied individually).
3. **Add a bed mode** to `docker/entrypoint.sh` mirroring the `--perm-smoke`/`--custom-tool` block: pick the
   single credential to KEEP, `strip_all_except` + `assert_clean_except`, run the driver under `timeout`,
   exit with its RC.
4. **Add the host wiring** to `docker/run.sh`: recognize the flag, inject exactly one credential, `exec docker
   run … "$IMAGE" --<mode>`.
5. **Run it** — real-credential lane for fidelity legs, free Ollama for plumbing, unit for code-truth. Record
   the green with evidence in the phase doc.
6. **Declare the flags** in the provider class per Part B, and add the (provider × capability) rows to the
   standing suite.

---

## Part F — The parity principle (C8/C9, later phases)

For the tiered observability/compaction contracts, **Claude SDK is the oracle**: capture Claude's OTEL
taxonomy / compaction split-points for a fixed task, then assert each other provider emits the **same
taxonomy** / compacts at **equivalent split points** and the task **still completes** — at the provider's
declared tier (PROV-3). OpenHarness "passing" C7 means turn-cap + wall-clock, not mid-turn dollars; that is
its declared tier, not a failure.

---

## Provenance

Written 2026-07-18 after Phases 1–3 (all three providers finalized). Grounded in the
capability-contracts build notes (internal, C1–C9), the provider-finalization phase plans
(internal — design-coverage + SDK-reality notes), and the live bed (`docker/run.sh`, `docker/entrypoint.sh`,
`docker/Dockerfile` + `tests/e2e/`). **Worked references, one per provider:**
[`providers/claude-sdk.md`](./providers/claude-sdk.md) (baseline oracle),
[`providers/openharness.md`](./providers/openharness.md) (in-proc, free lane),
[`providers/codex-sdk.md`](./providers/codex-sdk.md) (subprocess, approval bridge).
