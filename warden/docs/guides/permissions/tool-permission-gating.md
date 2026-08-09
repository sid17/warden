# Tool-call permission gating — regular vs custom tools, per provider

**What this guide answers:** when the harness's permission seam (`can_use_tool`) says *deny*, is
that decision actually honored? The answer is **yes for every regular tool on all three providers**,
and — **as of pre-07 / M9 (2026-07-22)** — **yes for custom tools on Claude and OpenHarness** too.
**Codex** is the one documented exception: its custom tools are ungated *by design* behind an explicit
opt-in (see the §3d finding below).

This is the difference between "a human-in-the-loop approval fires" and "the tool runs no matter what
you answer." It is load-bearing for durable HITL (M6):
building durable HTTP approval on top of a gate that a tool bypasses buys you nothing for that tool.

> **History:** this guide originally *proved the gap* (Claude + Codex custom tools were NOT gated).
> pre-07 · M9 **closed the
> Claude gap** with a scoped `PreToolUse` gating hook. The probe below is now a **regression guard**
> for parity, not a demonstration of the gap.

## The one-paragraph model

Permission in the harness is a **single seam**: the orchestrator owns one async callback,
`can_use_tool(tool_name, tool_input, …)` (`orchestrator/orchestrator.py`), which delegates to a
`PermissionHandler` (`seams/permissions.py`). Every provider bridges its *native* approval mechanism
into that one seam — Claude's SDK `can_use_tool` callback, OpenHarness's `PRE_TOOL_USE` hook, Codex's
fail-closed `approval_handler`. **Regular tools flow through the seam on all three.** Custom tools are
where it diverges: OpenHarness registers them as **native registry tools** (so they hit the same
`_execute_tool_call → PRE_TOOL_USE` gate as built-ins → **gated**), while Claude and Codex deliver
them over **MCP** — Claude auto-adds each custom tool to `options.allowed_tools`, which the SDK
*shadows* (auto-approves before the callback runs), and Codex routes them through MCP *elicitation*,
which never carries a `can_use_tool` decision. **pre-07 closes the Claude gap:** custom tools stay in `options.allowed_tools`
(so the model may call them), but a scoped `PreToolUse` hook — matcher `^mcp__harness_custom__`,
installed as a closure inside `install_hooks` — routes each custom-tool call back through the *same*
`can_use_tool` seam, so a deny now blocks it. The `CanUseToolShadowedWarning` for the custom-server
prefix is therefore expected (the hook, not the callback, is the gate) and is suppressed. Codex's
elicitation path remains ungated by design (§3d).

## The matrix (after pre-07 · M9)

Verified empirically (2026-07-22) on real models — Claude/Codex OAuth, OpenHarness free Ollama
`qwen3:8b` — with the probe below. Every regular tool blocked on deny; custom tools now gated on Claude
and OpenHarness.

| Provider | Regular tool | Custom tool | Why |
|---|---|---|---|
| **Claude** | ✅ **GATED** | ✅ **GATED** (pre-07) | custom tool stays in `options.allowed_tools` (SDK shadows `can_use_tool`), but a scoped `PreToolUse` gating hook (`providers/claude/session.py` `_build_custom_tool_gate`) re-routes it through `can_use_tool` — deny blocks it. Shadow warning for the custom prefix is expected + suppressed |
| **OpenHarness** | ✅ **GATED** | ✅ **GATED** | custom tool registered in the tool registry (`providers/openharness/session.py`) → executes via `_execute_tool_call` → `PRE_TOOL_USE` hook, the same gate as built-ins |
| **Codex** | ✅ **GATED** | ⚠️ **ungated by design** | custom tool rides the in-proc MCP **elicitation** path (`providers/codex/sdk_session.py`); the pinned SDK exposes **no answerable pre-execution MCP tool-call approval** (§3d), so custom tools fall back to the `codex_allow_ungated_custom_tools` policy: `False` (default) = **fail-closed** (raises at construction), `True` = **ungated auto-approve** (loud warning). exec/patch gating unaffected |

**Bottom line:** HITL / take-user-feedback now works on **regular tools everywhere** and **custom tools
on Claude + OpenHarness**. **Codex custom tools** are ungated by design (opt-in `True`) or fail-closed
(opt-in `False`) — never a late/partial gate.

## Reproduce it — the probe

Script: [`scripts/permission-gating-probe.py`](../../../scripts/permission-gating-probe.py). It installs
a **deny-all** `PermissionHandler` (via `config.permissions.handler_instance`) and runs two probes per
provider:

- **A — regular tool:** ask the model to write `regular_out.txt` with its native write tool. Gated ⇒
  file absent.
- **B — custom tool:** register `ping` (its handler writes `custom_ran.marker`) and ask the model to
  call it. Gate consulted ⇒ marker absent (blocked); gate bypassed ⇒ marker present (ran anyway).

```bash
cd "$(git rev-parse --show-toplevel)"
BASE=/tmp/permproof

# OpenHarness — free Ollama, predicts GATED / GATED
PYTHONPATH=. uv run --no-sync python warden/scripts/permission-gating-probe.py \
    --provider openharness --model qwen3:8b --base $BASE

# Claude — OAuth, predicts GATED / NOT gated (prints CanUseToolShadowedWarning)
env -u ANTHROPIC_API_KEY PYTHONPATH=. uv run --no-sync python \
    warden/scripts/permission-gating-probe.py --provider claude --base $BASE

# Codex — OAuth, predicts GATED / NOT gated
env -u OPENAI_API_KEY PYTHONPATH=. uv run --no-sync python \
    warden/scripts/permission-gating-probe.py --provider codex --base $BASE
```

### Reading the output (important)

The **authoritative signal is `handler consulted`** (did the deny-all gate get asked at all) **plus
the side-effect** (`regular_out.txt` / `custom_ran.marker` present?). Example — the two decisive lines:

```
# Claude, probe B (after pre-07) — the PreToolUse gate consulted the seam and blocked the tool:
[B custom tool]   handler consulted=['ping']
                  custom handler ran (marker)? False  => GATED (blocked)
# The SDK still warns that can_use_tool is shadowed for the custom prefix — that is now EXPECTED
# (the PreToolUse hook, not the callback, is the gate) and is suppressed by the provider.
```

```
# OpenHarness, probe B — the gate was asked and the tool was blocked:
[B custom tool]   handler consulted=['ping']
                  custom handler ran (marker)? False  => GATED (blocked)
```

> **Do not read gating off the `tool_use` event stream.** Only Claude reliably emits `tool_use`
> MessageEvents; codex/openharness leave it empty or `'?'`. A run can show `tool_use=[]` while the
> handler *was* consulted (`handler consulted=['Edit','Bash',…]`) and the tool *was* blocked — that's
> GATED, not inconclusive. The probe's verdict keys off `handler consulted` + side-effect for exactly
> this reason.

## Implications & status

- **Claude gap — CLOSED (pre-07 · M9).** The SDK's own warning named the fix — *"To gate every tool
  call, use a PreToolUse hook."* The harness now installs a **scoped** `PreToolUse` gating hook
  (`providers/claude/session.py` `_build_custom_tool_gate`, a closure inside `install_hooks` that
  captures `self._can_use_tool`), matcher `^mcp__harness_custom__` so regular tools are **not**
  double-gated. It runs alongside the audit/safety `PreToolUse` hooks (any `deny` wins).
- **Codex — ungated by design (§3d finding, 2026-07-22).** Verified against the pinned `openai_codex`
  0.144.4 binary: there is **no answerable pre-execution `item/mcpToolCall/requestApproval`** — MCP
  tool-call Guardian reviews are *notifications* (unanswerable), and the only answerable MCP seam
  (`mcpServer/elicitation/request`) would require our in-proc MCP server to emit a per-call
  approval-request elicitation from *inside the tool wrapper at execution time* = the in-`_wrapper`
  half-gate the plan explicitly rejects. So Codex custom tools fall back to the
  `codex_allow_ungated_custom_tools` policy (`True`=ungated auto-approve / `False`=fail-closed raise).
  exec/patch gating is unaffected and stays fail-closed.
- **For M6 (durable HITL):** durable HTTP pause/resume now gates **regular tools everywhere** and
  **custom tools on Claude + OpenHarness**. Codex custom-tool HITL is N/A (ungated-by-design); its
  built-in (exec/patch) HITL is fully supported.

## Conventions

- **Run Python** via `uv run --no-sync python` with `PYTHONPATH=.` from the repo root.
- **Never bill** — Claude/Codex OAuth, OpenHarness free local Ollama (`qwen3:8b`). Prefix with
  `env -u OPENAI_API_KEY` / `env -u ANTHROPIC_API_KEY` so a stray key can't switch you to the paid lane.
- The probe writes only into its `--base` scratch dir; it never touches the repo.
- Stray `Langfuse … Unauthorized (401)` lines are harmless telemetry noise (stale `LANGFUSE_*` env);
  the run continues without it. Unset `LANGFUSE_*` to silence.

## Related

- [`audit/audit-process.md`](../audit/audit-process.md) — translating audit findings into `PreToolUse`
  hooks + per-agent tool scoping (the same mechanism that closed the Claude custom-tool gap in pre-07).
- pre-07 · M9 — the module that
  gated Claude custom tools + recorded the Codex §3d finding.
- 07-durable-hitl.md
  — M6, the durable HTTP HITL that sits *above* this gate.
- `providers/README.md` — the per-provider `can_use_tool` bridge table.
