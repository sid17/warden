# Claude SDK provider — reference implementation design

> **Role.** The **baseline / reference oracle** for the harness provider contract. It was finalized first
> (Phase 1) and every other provider is measured against it: the parity contracts (OTEL taxonomy C8,
> compaction split-points C9) are literally defined as "emit the same taxonomy / compact at equivalent points
> as Claude for the same task." If a capability is ambiguous, *this* is the ground truth.
>
> **Read alongside:** [`../07-provider-contract-and-testing.md`](../07-provider-contract-and-testing.md) (the
> generic contract + bed methodology), the capability-contracts build note (internal)
> (C1–C9), and the code: `providers/claude/session.py`, `providers/base_provider.py`, `providers/auth.py`,
> `providers/claude/message_handler.py`, `schemas/providers.py`.
>
> **Status:** ✅ green on C1–C5 (Phase 1, 2026-07-18). C6–C9 tier-declared, wired in later phases.

---

## 1. What it is

`ClaudeSession` (`providers/claude/session.py`) is an **in-process** wrapper around the official
`claude_agent_sdk` (`ClaudeSDKClient`). "In-process" is nuanced: the SDK itself **spawns the `claude` CLI as
a child subprocess** and talks to it over a control protocol. So the harness code runs in-process, but the
model turn runs in a child the SDK owns — which is exactly why `crash_isolated = True` (a child crash
surfaces as a `ClaudeSDKError`, the host survives) yet `hard_kill_tier = "cooperative"` (the **SDK** owns the
child PID, so a hard stop is its cooperative `interrupt()`, not a harness-held `SIGKILL`). This is the PROV-4
correction — do **not** set `supports_hard_deadline = True` for Claude.

There is a **sibling adapter**, `ClaudeCliSession` (`claude-cli`, `providers/claude/cli_session.py`), which
wraps `claude -p` as a subprocess the harness itself spawns. Per **D7**, the SDK proved OAuth-headless +
permission enforcement in Docker, so `claude -p`'s reasons to exist are gone; it is **gated-in-place**
(kept, `mv`-only, retired at the factory later — never deleted). Its `ClaudeCliSession.send()` strip-then-inject
block (in `providers/claude/cli_session.py`) is the pattern that was **promoted into
`BaseProvider.apply_auth_env`** — the one transport that always
did per-run auth right, now the shared helper. (Its strip-then-inject block is `ClaudeCliSession.send()` in `providers/claude/cli_session.py`, looping `PROVIDER_AUTH_VARS["claude-cli"]`.)

### Capability flags (declared on the class; `ClaudeSession` class attrs in `providers/claude/session.py`)

| Flag | Value | Why |
|---|---|---|
| `crash_isolated` | `True` | SDK spawns a CLI child; a crash is contained |
| `hard_kill_tier` | `cooperative` | SDK owns the child PID → `interrupt()` (SIGTERM→SIGKILL), not a harness SIGKILL |
| `cost_visibility` | `mid_turn` | streamed `message_delta` cumulative tokens + terminal totals |
| `compaction` | `native` | SDK auto-compact is the **baseline** everyone else approximates |
| `supports_hard_deadline` | `False` | cooperative cancel, not a true mid-turn kill (PROV-4) |
| `custom_tool_delivery` | `in_proc_list` | in-proc SDK-MCP server via `create_sdk_mcp_server` |
| `perm_tier` | `arg_level` | T4-proven `can_use_tool` with full tool args |
| `retry_owner` | `sdk` | the Anthropic/Claude SDK retries transient errors (C4) |
| `max_output_tokens` | `None` | native compaction manages the window (not harness) |

---

## 2. Auth (C1/C2) — the `options.env` seam + strip-then-inject

**The seam.** The SDK merges `options.env` into the child process environment. That is the per-run credential
injection point — **not** process-global `os.environ`. This is what lets two concurrent sessions in one
process each carry a different key with no bleed (C2).

**The flow** in `ClaudeSession.start()`:
1. `options.env = build_claude_otel_env(self._telemetry)` — sets the native OTel vars (C8 baseline). `CLAUDE_CONFIG_DIR` is
   deliberately **not** in that set, so the session-home pin below never collides (R-SESS-1).
2. `options.env = self.apply_auth_env(options.env, "claude", self._auth_env)` — the `BaseProvider` helper:
   **strip every inherited Claude credential first**, then overlay `self._auth_env`. No-op when `auth_env is
   None` (single-user inherit path unchanged).
3. If `self._claude_config_dir` is set: `options.env["CLAUDE_CONFIG_DIR"] = str(dir)` — pins the session home
   (C4, below).

**Both modes (C1).** `auth_env` carries either `CLAUDE_CODE_OAUTH_TOKEN` (OAuth / subscription-login token)
**or** `ANTHROPIC_API_KEY`. Both are proven green in the clean container (`--auth oauth`, `--auth api-key`).

**AUTH-FIX — the strip set (`PROVIDER_AUTH_VARS` in `providers/auth.py`).** `PROVIDER_AUTH_VARS["claude"]` (and `claude-cli`) is now
`("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")`. The **critical addition is
`ANTHROPIC_AUTH_TOKEN`**: it is a bearer token the CLI honors, so an *inherited* one in `os.environ` would
**shadow an injected key** — stripping it first is the whole fix. Index `[0]` stays the OAuth token (the
"preferred for messaging" slot used by `auth_hint`). The strip set matches the bed's own
bed `entrypoint.sh` `CLAUDE_CRED_VARS` order (cloud-flags → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` →
`CLAUDE_CODE_OAUTH_TOKEN`).

**Isolation, proven (C2/T3).** `t3_isolation.py` drives the real `start()` path with two bogus keys + an
inherited `ANTHROPIC_AUTH_TOKEN` seeded in `os.environ`, then inspects each session's resolved `options.env`:
each carries only its own key, the inherited bearer is stripped, no OAuth bleed. Credential-free (bogus keys,
`connect()` neutralized) so it runs both locally and in the bed.

> **Nuance — auth vs permission ride different channels (D5).** `apply_auth_env` touches `options.env` only;
> `can_use_tool` rides a separate in-process callback. So the SDK gets per-run auth **and** per-run
> permissions together with no bridge. Isolation and control are not opposed.

---

## 3. Session home & lifecycle (C4)

- **`session_id`** is captured from the SDK's **first streamed message** (in `ClaudeSession.send()`), not
  pre-assigned. `resume_session_id` sets `options.resume` for a continued conversation.
- **Session-home pin (C4).** `claude_config_dir` → `CLAUDE_CONFIG_DIR`. When set, `discover_jsonl_path` scans
  **that** home's `projects/` dir instead of the shared `~/.claude/projects` — otherwise concurrent sessions
  in a shared home could pick up each other's transcript. `None` ⇒ unchanged (`~/.claude`).
- **N2 (factory).** The factory no longer pops `session_id` (`providers/__init__.py`) — it flows through for
  resume determinism. The SDK adapter stores a `_pin_session_id` but the SDK captures its own id, so the pin
  is dormant for this transport (used by `claude-cli`'s `--session-id`); harmless because the orchestrator
  never passes a bare `session_id` to a new SDK session.

---

## 4. Permissions (C3) — in-process, arg-level, fail-closed

The provider accepts `can_use_tool(name, input, ctx)` and hands it straight to `ClaudeAgentOptions.can_use_tool`
— an **in-process callback** the SDK invokes before a tool runs. The orchestrator's three-stage chain
(`ToolScope → PermissionChecker → PermissionHandler`, in `evaluate_tool_permission()` in
`orchestrator/permission_surface.py`, reached via `Orchestrator._can_use_tool`) is what that callback
executes; a denied tool is **blocked fail-closed with full args available** (`perm_tier = "arg_level"`), and
the deny reason feeds back so the model re-plans.

**Proven load-bearing (T4).** `t4_perm_smoke.py` isolates the callback from the SDK-native `disallowed_tools`
(forces that list empty) so a block can *only* come from the callback, then asserts: (a) callback fired, (b)
the denied file was **not** written, (c) the denial surfaced in the stream — plus an allow-control that *does*
write. Green in the bed.

`disallowed_tools` is also passed to `options.disallowed_tools` (SDK-native name-level block) as a
belt-and-suspenders static deny (N5).

---

## 5. Custom tools (TOOL-1/2) — in-proc SDK-MCP

Public API is a `list[CustomTool]` (`seams/custom_tools.py`: name/description/input_schema/handler). At
`start()`, `_wire_custom_tools` (`session.py`) adapts each one:

1. `_build_sdk_tool(ct)` wraps it as an SDK `@tool(ct.name, ct.description, ct.input_schema)` whose **async**
   handler calls `ct.handler(**args)`, awaits it if it returned a coroutine (**sync or async handlers both
   work**), and returns MCP text content `{"content": [{"type": "text", "text": str(res)}]}`.
2. `create_sdk_mcp_server("harness_custom", tools=[...])` → merged into `options.mcp_servers`.
3. Each tool's **fully-qualified MCP name `mcp__harness_custom__{name}`** is added to `options.allowed_tools`
   (the SDK gates MCP tools via `allowed_tools`).

**Why a list, not a standalone MCP server:** the in-process providers (Claude SDK, OpenHarness) reach the
model with a plain in-proc list — no standalone MCP transport needed for them (TOOL-2: a server only where
reach demands it; it doesn't here). Codex is the case where reach *does* demand a server: its subprocess can't
see an in-proc list, so custom tools ride an **in-proc streamable-HTTP MCP server** — but UNGATED (they can't
be permission-gated on Codex), behind the explicit `allow_ungated_custom_tools` opt-in. So Codex
`custom_tool_delivery` is an instance property = `mcp` (opted-in) / `none` (default → **must error** on
non-empty `custom_tools`, never drop). See [`codex-sdk.md`](./codex-sdk.md) §5.

**Proven (TOOL-1/2).** `t_custom_tool.py` registers `save_note` (writes a marker), runs a **real turn** that
forces the tool, and asserts the marker exists — green in the bed (`custom-tool-fired:hello from
t_custom_tool`). The structural precondition (`ct in sess._custom_tools`) proves consume-not-drop even before
the turn.

---

## 6. Cost & events (C5)

The terminal `ResultMessage` is normalized (in `transform_sdk_message()`, `providers/claude/message_handler.py`) into a `status`
event carrying **`totalCostUsd`**, **`usage`** (input / output / cache-read / cache-creation tokens),
`durationMs`, `numTurns`, `isError`. That is per-turn cost accounting (C5). `cost_visibility = "mid_turn"`
because the SDK also streams cumulative tokens via `message_delta` during the turn.

Event normalization is otherwise a **seam** today (`BaseProvider.normalize_event` = identity pass-through);
the concrete typed `Event` union (`stopped` / `compaction` / `tool_result`, each carrying
`parent_observation_id` for cross-process span nesting) is **N1 / Phase-4** work in `schemas/events.py`.

---

## 7. Hooks & observability

- **Audit hooks (AUD-1).** Env-activated: `AUDIT_ENABLED=1` builds `build_audit_hooks()` and merges them into
  `options.hooks` at `start()`. Independent of telemetry (AUD-1: audit and observability share no code/data).
- **`PreToolUse` path hook (SAFE-6).** Per-**path** Read/Write restriction must be a `PreToolUse` hook, not
  the permission callback — because `can_use_tool` does **not** fire for auto-allowed tools like `Read`. This
  is the second consumer of the generic `install_hooks` seam.
- **`install_hooks` seam.** `BaseProvider.install_hooks` is one generic override point for *both* the audit
  hook and the path hook (design-coverage §5.1). Phase 1 declares the seam; Claude's audit-hook install is
  still inline in `start()` — routing it through the seam is a noted follow-up.
- **OTEL (C8 baseline).** `build_claude_otel_env()` activates the SDK's **native** OTel — Claude's taxonomy
  for a given task is the canonical baseline every other provider is diffed against (T9). A Langfuse tracer
  (`ClaudeLangfuseTracer`) adds LLM analytics + sub-agent nesting.

---

## 8. What's tier-declared but not yet wired (C6–C9)

| Contract | Claude tier | Wired in |
|---|---|---|
| C6 interrupt on time | `cooperative` / `supports_hard_deadline=False` | governance phase (Governor seam, `06`) |
| C7 interrupt on cost | `mid_turn` visibility → can stop proactively | governance phase |
| C8 OTEL parity | native baseline (the oracle) | observability phase (parity diffs) |
| C9 compaction | `native` (observe + emit) | compaction phase (B25) + N1 `compaction` event |

These are **declared facts** now (the flags), so the layers above already know Claude's limits; the wiring
lands in the later phases against the Governor/observability seams.

---

## 9. Design choices & nuances (the "why", collected)

- **Protocol + capability flags + mixin, not a fat ABC.** Auth (`options.env`), permission (in-proc callback),
  and interrupt (cooperative) genuinely diverge across providers — forcing uniform bodies would be a leaky
  abstraction. The flags make divergence a *declared* fact; the mixin shares only the truly-common mechanics.
- **`options.env` is the credential seam** (merges into the child env) — the same seam OTel uses;
  `CLAUDE_CONFIG_DIR` is kept out of the OTel env set to avoid collision.
- **Strip-first is load-bearing**, not defensive politeness — without it an inherited `ANTHROPIC_AUTH_TOKEN`
  or OAuth token shadows the injected key and concurrent runs bleed.
- **`crash_isolated=True` but `supports_hard_deadline=False`** — the SDK contains a crash but the harness
  doesn't own the child PID, so it can't `SIGKILL`. Two different axes, correctly separated (PROV-4).
- **Sync/async custom-tool handlers both supported** — the `@tool` wrapper awaits only if the handler returned
  a coroutine.
- **`claude -p` gated-in-place, not deleted** (D7) — `mv`-only; its strip-inject pattern was promoted to the
  base rather than discarded.
- **No 8th capability flag** for sub-agent nesting — that's a wiring obligation gated by the T9 parity test.

---

## 10. File map (anchors, re-confirm before editing)

| File | What |
|---|---|
| `schemas/providers.py` | the `AgentProvider` Protocol + 7 flag properties + Literal aliases |
| `providers/base_provider.py` | `_reject_unknown_kwargs`, `apply_auth_env`, `describe_auth`/`install_hooks`/`normalize_event` seams |
| `providers/claude/session.py` | `ClaudeSession`: flags, `__init__` typed inputs, `start()` (auth/config-home/hooks/custom-tools), `send()`, `discover_jsonl_path` |
| `providers/auth.py` | `PROVIDER_AUTH_VARS` (AUTH-FIX), `resolve_auth`/`is_authed`/`auth_hint` |
| `providers/claude/message_handler.py` | SDK message → normalized event (cost/usage on `ResultMessage`) |
| `providers/claude/cli_session.py` | `claude-cli` sibling (gated-in-place, D7); origin of the strip-inject pattern |
| `orchestrator/permission_surface.py` | `evaluate_tool_permission()` — the three-stage chain `Orchestrator._can_use_tool` delegates to |
| `tests/e2e/{t4_perm_smoke,t3_isolation,t_custom_tool}.py` | the C3 / C2 / TOOL-1-2 bed drivers |
| `docker/{run.sh,entrypoint.sh,Dockerfile}` | the bed: `--auth`, `--perm-smoke`, `--custom-tool`, `--t3` |

---

*Written 2026-07-18 (Phase 1 close). This is the worked reference for
[`../07-provider-contract-and-testing.md`](../07-provider-contract-and-testing.md).*
