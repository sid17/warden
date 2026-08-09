# Provider Audit Mechanisms

> How audit works differently across the three providers — same schema, three
> mechanisms, three completeness levels.

## At a glance

**Claude is the baseline (richest). OpenHarness is close behind. Codex is
tool-calls-only.** All three write the **same** `AuditEvent` JSONL — they differ
only in *how much of the run they can see*.

| Provider | Mechanism | Captures | Does NOT capture |
|---|---|---|---|
| **Claude** | in-process SDK hooks | Full lifecycle (7 events): tool use + failures, subagent **start & stop**, turn stop, notifications — **plus the tool calls *inside* subagents** (nested via `agent_id`) | — (this is the baseline) |
| **OpenHarness** | subprocess command hooks | Most of the lifecycle (5 events): tool use + failures, subagent **stop**, turn stop, notifications | `SubagentStart`; **the tool calls made *inside* a subagent** (see below) |
| **Codex** | stream-derived (no hooks) | Tool calls only — `PreToolUse`/`PostToolUse` for command-executions | Everything else: no subagent events, no turn `Stop`, no failure event, no MCP custom tools |

**The subagent distinction (the crux — and the thing to get right):**

- **Claude** sees the subagent **and what happens inside it** — every tool call is tagged with the `agent_id` of the subagent that made it. → supports **per-subagent** least-privilege scoping.
- **OpenHarness** sees *that* a subagent ran (the `SubagentStop` **boundary**) but **not what it did inside** — the subagent's internal tool calls are a deferred gap. → knows subagents exist; **cannot** scope them by what they used.
- **Codex** has **no subagent concept** at all → root-level command inventory only.

So the common shorthand "OpenHarness captures everything except the subagent" is
slightly off: it captures the subagent's **existence/boundary**, just not its
**internals**. Only Claude sees inside a subagent.

**One provider-agnostic exception:** a governance stop (budget/deadline halt,
AUD-3) is recorded by the *runner* for all three — it is not a provider hook.

---

## The mental model

All three providers write the **same** `AuditEvent` JSONL schema
(`warden/schemas/audit.py`, serialized with OTel `gen_ai.*` dot-notation
keys via `to_jsonl_line()`). Because the schema is uniform, the downstream tools
work across all three without provider-specific code:

- `aggregate.py` — Tool Usage Matrix, Path Access Map, Command Inventory, Convergence
- `derive_manifest.py` — the per-sub-agent least-privilege manifest diff

But the three **obtain** those events by very different mechanisms, and with very
different **completeness**:

- **Claude** — a native SDK hook system, in-process async callbacks. Richest — the full lifecycle vocabulary.
- **OpenHarness** — a native COMMAND-hook system, each event spawning a subprocess. Full-ish — most of the lifecycle, minus one event and minus sub-agent internals.
- **Codex** — no hook system at all. The trail is **derived** from the normalized event stream. Tool-call-only.

Config is threaded uniformly: audit is gated on `config.observability.audit`
(`AuditConfig(enabled, run_id, log_dir)`). The only place `AUDIT_*` env is read
is the OpenHarness subprocess boundary (below).

---

## Claude — native SDK hooks (in-process, richest)

Implementation: `warden/observability/audit/claude_sdk_hooks.py`.

Claude registers async Python callbacks on `ClaudeAgentOptions.hooks`. The
callbacks run **in-process** — no subprocess, no serialization. `build_audit_hooks(run_id=…, log_dir=…)`
**closurizes** `run_id` + the `AuditLogWriter` at build time, so the callback
never touches env when it fires (zero env at fire time).

It registers the full lifecycle vocabulary — **7 event types**:

```
PreToolUse, PostToolUse, PostToolUseFailure,
SubagentStart, SubagentStop, Stop, Notification
```

Sub-agents **nest**: each event carries `agent_id` / `agent_type` from the hook
input, so the trail attributes tool calls to the specific sub-agent that made
them. This is what gives `derive_manifest.py` per-sub-agent granularity.

## OpenHarness — native command hooks (subprocess)

Implementation: `warden/observability/audit/openharness_hooks.py`
(registry) + `openharness_hook_handler.py` (the subprocess handler).

OpenHarness has a native hook system, but the hooks are **command** hooks: each
audit event spawns a subprocess —
`python -m warden.observability.audit.openharness_hook_handler` — that
writes one JSONL line. Because the writer is a child process, config must cross
the process boundary: the session **derives** `AUDIT_RUN_ID` / `AUDIT_LOG_DIR`
env *from* `AuditConfig` and injects it (`config → env → child`), and the
executor sets `$OPENHARNESS_HOOK_PAYLOAD` with the event body. The handler reads
those three env vars. This is serialization across a boundary — not ambient env
access.

It registers **5 events** (`_AUDIT_EVENTS`):

```
pre_tool_use, post_tool_use, subagent_stop, stop, notification
```

Two nuances vs Claude:

- **No `SubagentStart`** — only `SubagentStop` is captured.
- **No separate failure event** — `PostToolUseFailure` is *derived in the handler*: a `post_tool_use` payload with `tool_is_error=true` is written as `event_type="PostToolUseFailure"`.

**Sub-agent boundary, NOT internals (the key limit).** OpenHarness captures
*that* a sub-agent ran — the `subagent_stop` event marks the **boundary** — but
**not the tool calls made inside** that sub-agent's subprocess. The trail shows a
sub-agent existed and finished; its internal activity is invisible. This is the
one place OpenHarness materially trails Claude: Claude's in-process hooks fire for
the sub-agent's *own* tool calls and tag each with `agent_id`, so Claude sees the
full picture inside a sub-agent, whereas OpenHarness sees only the outline. Closing
this needs an OpenHarness library change — it is a deferred gap, the same boundary
limit the telemetry taxonomy documents. See
[../observability/baseline-taxonomy.md](../observability/baseline-taxonomy.md) §7
(sub-agent internals row).

Practical consequence: `derive_manifest.py` can propose **per-sub-agent** scoping
for Claude runs (it knows which tools each sub-agent used), but for OpenHarness it
can only attribute the **root's** tool usage — a sub-agent's internal tools never
reach the trail to be scoped.

## Codex — stream-derived (no hooks, tool-call-only)

Implementation: `warden/providers/codex/audit_tap.py`.

Codex has **no** native hook system — only an exec/patch approval callback plus a
notification event stream. So `CodexAuditTap` **derives** the audit trail from the
normalized event stream that `send()` already produces (`notification_to_event`):

- `tool_use` → `PreToolUse`
- `tool_result` → `PostToolUse`

(`run_id` + `AuditLogWriter` captured at build time; config-gated on `AuditConfig`, no env read.)

Completeness is the narrowest of the three:

- **Command-execution tool calls only.** No lifecycle events — no `SubagentStart`/`SubagentStop`, no `PostToolUseFailure`, no `Notification`.
- **No MCP custom tools.** Those ride the elicitation/approval path and are not surfaced as `tool_use` events, so they never reach this trail (documented gap).
- **Fail-closed.** codex exec is fail-closed by default, so a tool appears in the trail only if it *actually executed* (an approved command-execution).

Recently-fixed bug worth knowing: the SDK wraps each stream item in a pydantic
`RootModel` discriminated union (`payload.item.root` →
`CommandExecutionThreadItem` / `AgentMessageThreadItem` / …). Until the item was
unwrapped before reading its kind/fields, codex tool calls were **invisible** in
the trail. `sdk_message_handler._item_of()` now unwraps `.root`.

---

## Completeness matrix

Event type × provider. ✅ captured · ❌ not captured · n-a = mechanism has no such concept.

| Event type | Claude | OpenHarness | Codex | Note |
|------------|:------:|:-----------:|:-----:|------|
| `PreToolUse` | ✅ | ✅ | ✅ | Codex: command-execution tool calls only |
| `PostToolUse` | ✅ | ✅ | ✅ | Codex derives from `tool_result` |
| `PostToolUseFailure` | ✅ | ✅ (derived from `post_tool_use` + `tool_is_error`) | ❌ | Codex errors ride on the `PostToolUse` line's `error` field |
| `SubagentStart` | ✅ | ❌ | n-a | OH captures only stop; Codex has no sub-agent lifecycle in-stream |
| `SubagentStop` | ✅ | ✅ | n-a | OH: boundary only, internals deferred |
| `Stop` | ✅ | ✅ | ❌ | Codex has no turn-Stop hook |
| `Notification` | ✅ | ✅ | ❌ | — |
| governance-Stop (AUD-3) | ✅ | ✅ | ✅ | **Provider-agnostic** — recorded by the runner via `record.write_governance_stop()`, not a provider hook |

The last row is the exception to the mechanism split: a budget/deadline halt is a
Governor verdict folded by the runner, not a provider event, so `record.py`
writes it into the same per-run JSONL for **all three** providers.

---

## How to think about it

- **Two providers have real hook systems** (Claude in-process async callbacks, OpenHarness subprocess command hooks). They produce the full lifecycle vocabulary — tool calls *and* sub-agent + turn events.
- **The third (Codex) has no hooks**, so its trail is **stream-derived** and **tool-call-only**. Same schema, narrower completeness.
- **For least-privilege derivation** (`derive_manifest.py`): only **Claude** gives per-sub-agent *tool* detail — each event carries the `agent_id` of the sub-agent that made the call, so the tool can propose per-sub-agent scoping. **OpenHarness** sees sub-agent *boundaries* (`subagent_stop`) but not their internal tool calls, so it can attribute the **root's** tools but cannot scope a sub-agent by what it used. **Codex** gives the **root's** command inventory only (no sub-agent concept). In short: **per-sub-agent scoping is a Claude capability today**; OpenHarness and Codex derive at the root level.

Because the schema is identical, you never special-case a provider downstream:
run the same `aggregate.py` + `derive_manifest.py` over a mixed `logs/` dir. Just
read completeness with these mechanisms in mind — an absent event may mean "the
tool wasn't used" **or** "this provider's mechanism can't see it."
