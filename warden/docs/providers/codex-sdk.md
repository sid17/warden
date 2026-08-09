# Codex Python SDK provider — reference implementation design

> **Role.** The **OpenAI / Codex** slot — a NEW adapter (`CodexSdkSession`) on `openai-codex==0.144.4` that
> **subsumes** the old `codex exec` subprocess adapter. It implements the provider contract at the Codex tier:
> both auth modes, **fail-closed exec/patch permission gating**, full turn cycle, coarse cost, OS crash
> isolation. Custom tools cannot be permission-gated on codex, so they are delivered **UNGATED via an
> in-proc MCP server behind an explicit `allow_ungated_custom_tools` opt-in** (default off → error, never
> drop). exec/patch gating is unaffected (§5).
>
> **Read alongside:** [`../07-provider-contract-and-testing.md`](../07-provider-contract-and-testing.md)
> (generic contract + bed), [`claude-sdk.md`](./claude-sdk.md) (the baseline oracle), and — most importantly —
> the phase-3-sdk-reality build note (internal)
> (the **verified** SDK API; the earlier research prose was stale). Code: `providers/codex/sdk_session.py`,
> `providers/codex/sdk_message_handler.py`, `providers/auth.py`, `providers/__init__.py`.
>
> **Status:** ✅ green at tier (Phase 3, 2026-07-18) — real bed legs `--codex-auth` (OAuth + API-key) and
> `--codex-perm` (T4 exec/patch) PASS.

---

## 1. What it is

`CodexSdkSession` (`providers/codex/sdk_session.py`) drives the official Codex Python SDK, which shells the
`codex` binary as a subprocess and speaks JSON-RPC over stdio. Because the harness owns that subprocess,
`crash_isolated = True` **and** `hard_kill_tier = "os"` (a real `SIGKILL` of a harness-owned PID — stronger
than Claude's cooperative tier). The old `codex exec` adapter (`providers/codex/session.py`) is **kept**
(mv-only) but gated at the factory under the legacy key `codex-exec`; the canonical `codex` provider is now
this SDK adapter.

### Capability flags (class attrs on `CodexSdkSession`; `custom_tool_delivery` is the instance `@property` below)

| Flag | Value | Why |
|---|---|---|
| `crash_isolated` | `True` | SDK shells the `codex` binary as a subprocess; a crash is contained |
| `hard_kill_tier` | `"os"` | harness owns the client subprocess PID → true `SIGKILL` |
| `cost_visibility` | `"coarse"` | usage arrives on a session-level event, not streamed per-token |
| `compaction` | `"harness_driven"` | `thread.compact()` at split points (C9) |
| `supports_hard_deadline` | `True` | true OS kill of a harness-owned PID |
| `custom_tool_delivery` | `"mcp"` / `"none"` | INSTANCE `@property` (`CodexSdkSession.custom_tool_delivery`): `"mcp"` when `allow_ungated_custom_tools` set + tools present (ungated in-proc HTTP MCP delivery), else `"none"` (default → `custom_tools` raises, never drops). `jsonl_path` **is** populated post-turn by `discover_jsonl_path()` (below) |
| `perm_tier` | `"arg_level"` | handler ON: exec/patch fail-closed with args — **covers exec/patch ONLY**; MCP custom tools ride the elicitation accept path and are NOT gated by `can_use_tool` (§5) |
| `retry_owner` | `"sdk"` | the openai/codex SDK retries transient errors (C4) |
| `max_output_tokens` | `None` | SDK/native compaction manages the window |

---

## 2. The hard part — permission gating (C3), and why it's built the way it is

This is the load-bearing capability and the whole reason the adapter is shaped as it is. **Three verified
facts drove the design** (all in the SDK-reality note):

1. **Approvals work only on the low-level *sync* `CodexClient`.** The async client and the high-level
   `Codex`/`AsyncCodex` do **not** accept an `approval_handler`; `CodexConfig` has no approval field. So the
   adapter builds the high-level `Codex(config)` off the event loop and then **injects its own handler onto
   the internal sync client**: `self._codex._client._approval_handler = self._approval` (in `CodexSdkSession.start()`).
2. **The default handler is FAIL-OPEN** (`_default_approval_handler` returns `accept`). The harness must
   always supply its own — a Codex adapter with no handler is the *reduced* profile (PERM-3), never a silent
   disable.
3. **`ApprovalMode.auto_review` — and even `AskForApproval(on_request)` — AUTO-APPROVE exec/patch before the
   handler is consulted** (verified empirically: handler fired 0×, file written). The fix that makes the
   handler load-bearing is **`approval_policy=untrusted` + `approvals_reviewer=None`** (in
   `CodexSdkSession._start_thread()`). The public `approval_mode` param is accepted for parity but overridden.

### The sync→async approval bridge (`CodexSdkSession._approval()`)

The handler is **synchronous and runs on the SDK's reader thread**, while the orchestrator's `can_use_tool`
is async on the event loop. `start()` captures `self._loop = asyncio.get_running_loop()`; the sync handler
then:
1. `approval_to_tool_call(method, params)` maps the approval → `(tool_name, tool_input)` (unknown method →
   **decline**).
2. If no callback/loop wired → **decline** (reduced profile is fail-closed here).
3. `fut = asyncio.run_coroutine_threadsafe(can_use_tool(tool_name, tool_input, None), self._loop)`;
   `result = fut.result(timeout=30s)`.
4. `{"decision": "accept"}` only when `behavior == "allow"`, else `{"decision": "decline"}`. **Any
   exception/timeout → decline.** Every branch fails closed.

### Approval `params` → `tool_input` mapping (`approval_to_tool_call()` in `sdk_message_handler.py`, captured from real turns)

- `item/commandExecution/requestApproval` → `("Bash", {command, cwd, item_id, thread_id, turn_id, ...})` —
  `command` is the full argv string (e.g. `/bin/zsh -lc '…'`).
- `item/fileChange/requestApproval` → `("Edit", {reason, grant_root, item_id, ...})` — note the approval
  carries the escalation *reason*/`grantRoot`, **not** the patch body/path (those ride the streamed
  file-change item).

**Proven (T4):** `codex_perm_smoke.py` in the clean bed — deny an exec, force it → handler fired, action
**blocked** (file absent); allow-control → handler fired, file written.

---

## 3. Auth (C1) — both modes

`CodexSdkSession.start()` (`CodexConfig(...)` built in `_build_codex()`) sets `cwd=repo`, `env={**auth_env, CODEX_HOME=...}`:
- **API key:** `auth_env["OPENAI_API_KEY"]` present → `await asyncio.to_thread(codex.login_api_key, key)`.
- **OAuth (ChatGPT subscription):** the binary reads `CODEX_HOME/auth.json` directly (device-code login
  persists there) — pin `CODEX_HOME` in `CodexConfig.env`; no explicit login call needed.
- Per-run isolation: `auth_env` is injected into the subprocess env (analogue of Claude's `options.env`).

**N4 fix (`providers/auth.py`):** `is_authed('codex')` now returns True when `CODEX_HOME/auth.json` (or
`~/.codex/auth.json`) exists, not only when `OPENAI_API_KEY` is set — via a `_codex_auth_present(env)` file
probe. `resolve_auth` stays **pure** (no file reads; it only surfaces env vars).

**Proven (C1):** `--codex-auth` PASS in the clean bed in **both** modes — API-key (`OPENAI_API_KEY`) and OAuth
(mounted `auth.json`, cache-read tokens confirm a real session).

---

## 4. Turn cycle, streaming & cost (C4/C5)

Everything blocking runs off the loop via `asyncio.to_thread`:
- `start()`: build `Codex` (its ctor starts + initializes the client) off-loop; inject the handler; start the
  thread under the fail-closed policy → `session_id` = the codex thread id.
- `send(prompt)` (`CodexSdkSession.send()`): run `thread.turn(prompt)` in a thread; **pump `TurnHandle.stream()`
  (a sync `Iterator[Notification]`) into an `asyncio.Queue`** that the async generator drains and `yield`s
  (normalized via `sdk_message_handler`).
- **Cost (C5, coarse):** usage arrives on the `thread/tokenUsage/updated` notification
  (`.token_usage.total`) — **not** on `turn/completed` (a bug found + fixed during verification). Surfaced on
  the terminal status event.

---

## 5. Custom tools — ungated MCP delivery behind an explicit opt-in

Codex custom tools **cannot be permission-gated**: they ride the MCP elicitation approval path
(`mcpServer/elicitation/request`), which does not carry a per-call `can_use_tool` decision (verified —
the elicit form is a coarse server-level allow, and the response shape is the MCP elicit-result
`{"action": …}`, not the exec/patch `{"decision": …}`). So custom tools are delivered **UNGATED**, behind
an explicit opt-in. **Default stays fail-closed (error).**

- **Delivery mechanism.** `providers/codex/custom_tool_mcp_server.py` (`CustomToolMcpServer`) runs an
  **in-process streamable-HTTP MCP server** (`FastMCP.run_streamable_http_async()`, bound `127.0.0.1` on an
  ephemeral port) that registers each `CustomTool` as an MCP tool — the handlers stay in harness memory.
  Codex (subprocess) connects to it by URL, injected as an `mcp_servers` entry via the low-level
  `CodexConfig.config_overrides` (`mcp_servers.harness_custom.url="http://127.0.0.1:{port}/mcp"` — the exact
  shape `codex mcp add --url` writes; see the phase-3 SDK-reality build note (internal)).
- **The opt-in.** `CodexSdkSession(..., allow_ungated_custom_tools: bool = False)`.
  - **Off (default) + `custom_tools` → RAISE** at construction, naming the flag (TOOL-1: consume-or-error,
    never silently drop). The factory no longer special-cases codex (`_CUSTOM_TOOLS_UNSUPPORTED` is empty);
    the adapter owns the fail-closed raise.
  - **On → start the MCP server, inject the config, and log a LOUD warning** that these tools bypass
    permission gating. The `_approval` handler auto-accepts `mcpServer/elicitation/request` with
    `{"action": "accept"}` **only when opted-in**; without the opt-in that method declines
    (`{"action": "decline"}`).
- **exec/patch gating is untouched.** `item/commandExecution/requestApproval` /
  `item/fileChange/requestApproval` still route through the fail-closed `can_use_tool` bridge (§2) exactly
  as before — the opt-in changes ONLY MCP custom-tool delivery.
- **Config bit.** `CODEX_ALLOW_UNGATED_CUSTOM_TOOLS` (`HarnessSettings.codex_allow_ungated_custom_tools`,
  `ProviderConfig.codex_allow_ungated_custom_tools`), threaded to the adapter via
  `ChatAPI → Orchestrator → provider_kwargs["allow_ungated_custom_tools"]` (codex only). The constructor
  param stays authoritative.
- **Proven.** Unit + a real e2e (`tests/e2e/codex_custom_tool_smoke.py`, ran locally: codex called the
  in-proc handler, marker written) + bed leg `--codex-custom-tool`.

---

## 6. Retirement & factory (D6/D7)

`providers/__init__.py`: `provider == "codex"` → `CodexSdkSession` (subsume). The old exec `CodexSession`
file is **kept** (mv-only) and reachable only under `codex-exec`, which raises `NotImplementedError`.
`claude-cli` is likewise gated (D7). **Consequence handled:** gating `claude-cli` required repointing the
`harness_api` default provider (`RunSpec.provider`) from `claude-cli` → `claude`
(`harness_api/schemas.py`).

---

## 7. Design choices & nuances (the "why", collected)

- **Use the sync low-level `CodexClient`, not the async client** — it is the *only* place approvals wire in.
  The high-level `Codex` gives the ergonomic `thread.turn().stream()`; the adapter uses both (build high-level,
  inject the handler onto its internal sync client).
- **`approval_policy=untrusted` is non-negotiable** — `auto_review`/`on_request` auto-approve; without
  `untrusted` + `approvals_reviewer=None` the gate is silently fail-open. Verified, not assumed.
- **Every approval branch fails closed** — unknown method, no callback, exception, timeout all → `decline`.
- **Cost is on `tokenUsage/updated`, not `turn/completed`** — a real, easy-to-miss detail.
- **Custom tools are UNGATED by nature on codex** — they ride the elicitation path that can't carry a
  `can_use_tool` decision, so delivery is behind the explicit `allow_ungated_custom_tools` opt-in (default
  off → raise, never drop, TOOL-1). When on, they're delivered via an in-proc streamable-HTTP MCP server
  and the elicitation is auto-accepted; exec/patch stay gated (§5).
- **Reduced profile is a named, fail-closed mode** — no handler = decline everything, not auto-accept.

---

## 8. File map (anchors, re-confirm before editing)

| File | What |
|---|---|
| `providers/codex/sdk_session.py` | `CodexSdkSession`: capability-flag class attrs, `custom_tool_delivery` `@property`, `discover_jsonl_path()` (populates `jsonl_path` post-turn by exact thread-id match), `start()`, `send()`/stream pump, `_approval()` bridge, `_start_thread()` untrusted policy, `stop`/`close` (OS-kill) |
| `providers/codex/sdk_message_handler.py` | `approval_to_tool_call()` + `notification_to_event()` normalization |
| `providers/codex/custom_tool_mcp_server.py` | `CustomToolMcpServer`: in-proc streamable-HTTP MCP server (127.0.0.1, ephemeral port) mapping `CustomTool`→FastMCP tool; `start()`→URL / `stop()` |
| `providers/codex/session.py` | old `codex exec` adapter — **kept, gated** (mv-only, `codex-exec`) |
| `providers/auth.py` | N4 `is_authed('codex')` file probe; `resolve_auth` kept pure |
| `providers/__init__.py` | factory: `codex`→SDK, `codex-exec`/`claude-cli` gated |
| `tests/e2e/{codex_auth_smoke,codex_perm_smoke,codex_custom_tool_smoke}.py` | the C1 / C3(T4) / ungated-custom-tool bed drivers |
| `tests/providers/test_codex_mcp_tools.py` | unit: ungated opt-in gate, MCP server mapping, `_approval` elicitation path |
| `docker/{run.sh,entrypoint.sh,Dockerfile}` | bed modes `--codex-auth`, `--codex-perm`, `--codex-custom-tool` |

---

*Written 2026-07-18 (Phase 3 close). Verified SDK surface: the phase-3-sdk-reality build note (internal).*
