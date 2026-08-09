# OpenHarness provider — reference implementation design

> **Role.** The **open / portable** slot — an **in-process** adapter (`OpenHarnessSession`) wrapping the
> `openharness` library's `QueryEngine` against a local Ollama (OpenAI-compatible) endpoint. It implements the
> provider contract at its tier: full turn cycle, **arg/path-level permissions**, custom tools, per-run
> `api_key`, terminal-only cost. It is the **cheapest validation lane** — free to test on Ollama, in-process
> like the Claude baseline — so it shakes out the contract without spending cloud credits.
>
> **Read alongside:** [`../07-provider-contract-and-testing.md`](../07-provider-contract-and-testing.md)
> (generic contract + bed) and [`claude-sdk.md`](./claude-sdk.md) (the baseline oracle). Code:
> `providers/openharness/session.py`, `permission_bridge.py`, `custom_tool_adapter.py`.
>
> **Status:** ✅ green at tier (Phase 2, 2026-07-18) — free Ollama lane + Docker bed (`--openharness-perm`) PASS.

---

## 1. What it is

`OpenHarnessSession` (`providers/openharness/session.py`) runs the `openharness` `QueryEngine` **in the
harness process** against Ollama's OpenAI-compatible API. Being truly in-process (no subprocess, shared
Ollama daemon), it declares `crash_isolated = False` and `hard_kill_tier = "none"` — it has **neither** crash
isolation nor a hard kill; runs are bounded by turn-cap + wall-clock instead (governance). This is the
honest, *declared* tier, not a gap.

### Capability flags (class attrs on `OpenHarnessSession`)

| Flag | Value | Why |
|---|---|---|
| `crash_isolated` | `False` | truly in-process; shared Ollama daemon |
| `hard_kill_tier` | `"none"` | no PID to kill; bounded by turn-cap + wall-clock |
| `cost_visibility` | `"terminal"` | post-hoc usage only; not dollars, not mid-turn |
| `compaction` | `"harness_driven"` | summarize-then-resume at split points (C9) |
| `supports_hard_deadline` | `False` | wall-clock + `num_predict`, no true kill |
| `custom_tool_delivery` | `"in_proc_list"` | registered into the `tool_registry` |
| `perm_tier` | `"arg_level"` | `can_use_tool` runs as a `PRE_TOOL_USE` hook (B15 closed) |
| `retry_owner` | `"harness"` | bare Ollama/LiteLLM transport → harness owns backoff (C4) |
| `max_output_tokens` | `4096` (`_MAX_OUTPUT_TOKENS`) | the governance backstop (no native compaction) |

---

## 2. The hard part — arg-level permissions (C3 / B15), and the DEFAULT-mode subtlety

This was the main gap and the trickiest wiring. **The old bridge was name-level only:** OpenHarness's
`permission_prompt` seam is `Callable[[tool_name, reason], bool]` — it structurally cannot carry
`tool_input`, so an arg/path rule (e.g. deny a *path*) could never fire (the seam saw `{}` while the real
call carried `{path, content}`).

**The fix (B15): route the decision through the `PRE_TOOL_USE` hook**, which the query loop fires with the
FULL `{tool_name, tool_input}` before `tool.execute` (`openharness.engine.query._execute_tool_call`, which
blocks on `pre_hooks.blocked`).

- `build_permission_hook(can_use_tool)` (`permission_bridge.py`) returns a hook adapter: receive
  `{tool_name, tool_input}`, `await can_use_tool(tool_name, tool_input, None)`, return
  `HookResult(blocked = behavior != "allow")`, **fail-closed on exception**.
- Upstream's stock `HookExecutor` only dispatches *declarative* hooks (command/http/prompt/agent) — there's
  no arbitrary-callable path. So rather than fork upstream, **`PermissionHookExecutor`** wraps the opaque
  `async execute(event, payload) -> AggregatedHookResult` contract: on `PRE_TOOL_USE` it runs the permission
  check and **merges** with any audit-executor results; other events delegate straight to the audit executor
  (audit hooks keep firing).

### The DEFAULT-mode auto-confirm (the non-obvious correctness fix)

The naive "single gate via the hook, `permission_prompt=None`" **breaks all mutations**. `_execute_tool_call`
runs the hook FIRST (in `_execute_tool_call`), THEN `PermissionChecker.evaluate()`. In DEFAULT mode `evaluate()`
returns `requires_confirmation=True` for *every* mutating tool, and with no `permission_prompt` that path
becomes an **unconditional block** — the model loops on `/permissions full_auto` and custom tools never run.

**Fix:** `permission_prompt = build_auto_confirm_prompt()` — auto-approve the upstream DEFAULT-mode ceremony.
This is safe (no hole) because:
- the hook already made the **arg-aware orchestrator decision** and blocks a denied tool *before* the checker;
- the checker's OWN hard denies (sensitive paths, explicit deny rules, command patterns) return
  `allowed=False` **without** `requires_confirmation`, so they still block and never reach the prompt.

No double-deny: the hook = orchestrator policy, the prompt = upstream ceremony. **Verified** by the deny legs
staying blocked while the allow-control writes.

If no `can_use_tool` is supplied (standalone use), the session falls back to `FULL_AUTO` with a loud warning —
there is no policy to enforce.

**Proven (C3/PERM-1/B15):** `openharness_perm_smoke.py` on the free Ollama lane + the `--openharness-perm` bed
gate — name-level deny blocked, **arg/path-level deny with the seam seeing the real `{path, content}`**
blocked, allow-path control written, custom tool executed.

---

## 3. Custom tools (TOOL-1/2) — in-proc `tool_registry`

`CustomToolAdapter(BaseTool)` (`custom_tool_adapter.py`) wraps each harness `CustomTool` as an OpenHarness
`BaseTool`: it advertises the tool's JSON-Schema via a `to_api_schema()` override, uses a lenient
passthrough `input_model`, and runs the (sync-or-async) `handler`, surfacing failures as `ToolResult(...,
is_error=True)`. Adapters are registered into the registry after `create_default_tool_registry()`
(`session.py`). Delivery = `in_proc_list` (TOOL-2 — a list is valid because the provider is in-process).

Phase 1 had OpenHarness *raise* on custom tools; Phase 2 wires consumption and removes both the raise and
`openharness` from the factory's `_CUSTOM_TOOLS_UNSUPPORTED` guard.

---

## 4. Auth (C1, at tier) & session-home

OpenHarness has **no cloud credential** (local Ollama), so there's no OAuth mode and `apply_auth_env` doesn't
apply. Per-run auth is the `api_key` constructor arg into `OpenAICompatibleClient`: resolution order is
explicit `api_key` → a key from `auth_env` (`OPENAI_API_KEY`/`OPENHARNESS_API_KEY`) → typed settings →
`"ollama"` dummy (local Ollama accepts any non-empty key). A managed key from `auth_env` becomes *this run's*
credential — in-process (not env), the logical analogue of per-instance isolation.

**Session-home:** a `session_home` param pins the transcript + JSONL-discovery dirs into the per-task unit via
`OpenHarnessSession._session_root()`; `None` ⇒ the global `~/.openharness` (unchanged). Turns are bounded by
`max_turns=8` + `max_tokens=self._MAX_OUTPUT_TOKENS` (= `4096`, the governance backstop, since there's no
hard kill).

---

## 5. Cost & events (C5, terminal)

Usage is available only post-hoc (`cost_visibility = "terminal"`) — OpenHarness does not stream mid-turn
dollars, and there are no dollars at all on local Ollama. The Governor governs it by **turns + wall-clock**
(GOV-3), not by a mid-run cost cap. A Langfuse tracer adds LLM analytics.

> **Known image-deps note:** the Docker bed image currently lacks `opentelemetry`, so OpenHarness OTel init
> logs a **non-fatal** error and continues. Pre-existing (the default four-provider leg hits it too);
> observability wiring is a later phase.

---

## 6. Design choices & nuances (the "why", collected)

- **Route permissions through the `PRE_TOOL_USE` hook, not `permission_prompt`** — the prompt seam is 2-arg
  and can't carry `tool_input`; the hook carries full args, fires before execute, and covers every tool. No
  fork/upstream-PR needed.
- **Wrap the executor (`PermissionHookExecutor`) instead of registering a callable** — upstream's registry
  only holds declarative `HookDefinition`s; there is no arbitrary-callable hook type.
- **`build_auto_confirm_prompt()` is load-bearing, not a bypass** — DEFAULT mode blocks all mutations without
  it; the hook already made the real decision, and the checker's hard denies still block. Verified no hole.
- **Fail-closed everywhere** — a raising `can_use_tool` in the hook → blocked; no callback → `FULL_AUTO` only
  as an explicit standalone fallback with a loud warning.
- **Honest tier declaration** — `crash_isolated=False`, `hard_kill_tier=none`, `cost_visibility=terminal` are
  *declared* facts so the Governor uses the turn-cap+wall-clock path, never a false hard-stop.

---

## 7. File map (anchors, re-confirm before editing)

| File | What |
|---|---|
| `providers/openharness/session.py` | `OpenHarnessSession`: capability-flag class attrs (incl. `_MAX_OUTPUT_TOKENS=4096`), `start()` permission/hook assembly, `send()`, `_session_root()` |
| `providers/openharness/permission_bridge.py` | `build_permission_hook`, `build_auto_confirm_prompt`, `PermissionHookExecutor` (+ deprecated `build_permission_prompt`) |
| `providers/openharness/custom_tool_adapter.py` | `CustomToolAdapter(BaseTool)` — CustomTool → OpenHarness tool |
| `providers/__init__.py` | factory (openharness dropped from the custom-tools guard) |
| `tests/e2e/openharness_perm_smoke.py` | the free-lane B15 + custom-tool driver |
| `docker/{run.sh,entrypoint.sh,Dockerfile}` | bed mode `--openharness-perm` (host Ollama, 600s budget) |
| upstream: `openharness/engine/query.py` `_execute_tool_call()` | the `PRE_TOOL_USE` fire site (blocks before `tool.execute`) |

---

*Written 2026-07-18 (Phase 2 close). Worked reference for [`../07-provider-contract-and-testing.md`](../07-provider-contract-and-testing.md); baseline: [`claude-sdk.md`](./claude-sdk.md).*
