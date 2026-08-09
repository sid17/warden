# Testing Audit Hooks

How to verify that audit JSONL logging works across the three providers after code changes.

Audit is config-first: enabled via `config.observability.audit` (`AuditConfig`),
which `AUDIT_ENABLED`/`AUDIT_RUN_ID`/`AUDIT_LOG_DIR` populate through the settings
layer. The OpenHarness hook subprocess is the one place that reads `AUDIT_*` env
directly — that is the `config → env → child` boundary, not ambient env access.
See [provider-audit-mechanisms.md](provider-audit-mechanisms.md).

## Prerequisites

| Service | Port | Check |
|---------|------|-------|
| Ollama | :11434 | `curl -s http://localhost:11434/api/tags` → model list (for smoke tests) |

No Langfuse or OTel Collector needed — audit is independent of telemetry.

```bash
cd /path/to/repo-root   # the uv workspace root
export PYTHONPATH=.
```

---

## Layer 1: Unit Tests (no Ollama, no network)

Fast, deterministic tests for event mapping, JSONL format, and registry construction.

### Claude SDK audit tests

```bash
PYTHONPATH=. uv run --no-sync python -m pytest warden/tests/observability/audit/test_hooks.py -v
```

**Pass criteria:**
- [ ] `_record_event()` maps all 7 Claude event types (PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop, Stop, Notification)
- [ ] `build_audit_hooks(run_id=…, log_dir=…)` returns a dict with all 7 event types, each with one `HookMatcher`
- [ ] JSONL output uses OTel dot-notation keys (`gen_ai.operation.name`, not `gen_ai_operation_name`)

### OpenHarness audit tests

```bash
PYTHONPATH=. uv run --no-sync python -m pytest warden/tests/observability/audit/test_openharness_hooks.py -v
```

**Pass criteria:**
- [ ] `handle_payload()` maps all 5 OH event types → correct `event_type` values
- [ ] `post_tool_use` with `tool_is_error=true` → `PostToolUseFailure` (derived in-handler; OH has no separate failure event)
- [ ] Tool name mapping: `write_file` → content stripped from `tool_input_summary`
- [ ] `main()` reads `$OPENHARNESS_HOOK_PAYLOAD` + `AUDIT_RUN_ID`/`AUDIT_LOG_DIR` and writes JSONL
- [ ] Invalid JSON payload doesn't crash (audit never blocks pipeline)
- [ ] JSONL format identical to Claude SDK (same OTel dot-notation keys)

### Codex audit tap tests

```bash
PYTHONPATH=. uv run --no-sync python -m pytest warden/tests/observability/audit/test_hooks.py -v -k codex   # if present
```

Codex has no native hooks — `CodexAuditTap.record()` derives `PreToolUse`/`PostToolUse`
from `tool_use`/`tool_result` stream events. See its coverage tests and the
[provider-audit-mechanisms.md](provider-audit-mechanisms.md) matrix.

### The derivation + record tests

```bash
PYTHONPATH=. uv run --no-sync python -m pytest \
  warden/tests/observability/audit/test_derive_manifest.py \
  warden/tests/observability/audit/test_record.py \
  warden/tests/observability/audit/test_config_gate.py -v
```

**Pass criteria:**
- [ ] `derive_manifests()` proposes least-privilege diffs; root + `<0.8`-stability agents kept broad
- [ ] `write_governance_stop()` records a terminal `Stop` (AUD-3) when audit is on, no-op when off
- [ ] Audit is a no-op unless `AuditConfig.enabled` is true (config gate)

---

## Layer 2: Hook Registration (no Ollama)

Verify the registry constructors produce valid objects without runtime errors.

### Claude SDK

```bash
PYTHONPATH=. uv run --no-sync python -c "
from warden.observability.audit.claude_sdk_hooks import build_audit_hooks
hooks = build_audit_hooks(run_id='reg-check')
for et, matchers in sorted(hooks.items()):
    m = matchers[0]
    print(f'  {et:22s} matcher={m.matcher}  hooks={len(m.hooks)}  timeout={m.timeout}')
print(f'Total: {len(hooks)} event types')
"
```

**Pass criteria:**
- [ ] 7 event types listed
- [ ] All have `matcher=None`, `hooks=1`, `timeout=5.0`

### OpenHarness

```bash
PYTHONPATH=. uv run --no-sync python -c "
from warden.observability.audit.openharness_hooks import build_openharness_audit_hooks
from unittest.mock import MagicMock
from pathlib import Path

executor = build_openharness_audit_hooks(Path('.'), MagicMock(), 'qwen3:1.7b')
registry = executor._registry

from openharness.hooks.events import HookEvent
for event in HookEvent:
    hooks = registry.get(event)
    if hooks:
        h = hooks[0]
        print(f'  {event.value:20s} type={h.type}  block_on_failure={h.block_on_failure}  command={h.command[:50]}...')
print(f'Total: {sum(1 for e in HookEvent if registry.get(e))} event types')
"
```

**Pass criteria:**
- [ ] 5 event types: `pre_tool_use`, `post_tool_use`, `subagent_stop`, `stop`, `notification`
- [ ] All have `type=command`, `block_on_failure=False`
- [ ] Command starts with the current Python interpreter path (`sys.executable`)
- [ ] No hooks registered for `session_start`, `session_end`, `pre_compact`, `post_compact`, `user_prompt_submit`, `subagent_start`

---

## Layer 3: Smoke Tests (needs Ollama + qwen3:1.7b)

End-to-end integration tests that run real prompts and validate JSONL output.

### Run all OpenHarness smokes

```bash
PYTHONPATH=. uv run --no-sync python -m warden.tests.observability.audit.test_openharness_smoke
```

### Run individual smokes

```bash
# Fastest — start here
PYTHONPATH=. uv run --no-sync python -m warden.tests.observability.audit.test_openharness_smoke 1

# All hard-gate smokes
PYTHONPATH=. uv run --no-sync python -m warden.tests.observability.audit.test_openharness_smoke 1 2 3 4 5

# Best-effort agent test
PYTHONPATH=. uv run --no-sync python -m warden.tests.observability.audit.test_openharness_smoke 6
```

### What each smoke tests

| Smoke | Prompt | Expected events | What it validates |
|-------|--------|----------------|-------------------|
| 1 | `echo hello_audit_test` | PreToolUse + PostToolUse | Basic tool audit capture |
| 2 | Read CLAUDE.md | PreToolUse + PostToolUse | File read tool, input/output summaries |
| 3 | Read + wc -l | ≥4 events (2 tool pairs) | Multi-tool turn, timestamps ascending |
| 4 | Write /tmp file | PreToolUse + PostToolUse | Write tool content stripping (tool name mapping) |
| 5 | echo smoke5_done | PreToolUse + PostToolUse + Stop | Stop event at end of turn |
| 6 | Agent tool call | SubagentStop | Sub-agent lifecycle (best-effort, qwen3:1.7b unreliable) |

**Hard gate:** Smokes 1–5 must pass. Smoke 6 is best-effort.

### Manual JSONL inspection

After running a smoke, inspect the raw output:

```bash
PYTHONPATH=. uv run --no-sync python -c "
import json
with open('warden/observability/audit/logs/oh-smoke-1.jsonl') as f:
    for line in f:
        e = json.loads(line)
        tool = e.get('tool_name', '')
        print(f\"{e['event_type']:20s} tool={tool:20s} gen_ai.op={e.get('gen_ai.operation.name','')}\")
"
```

### Using the validation function directly

```bash
PYTHONPATH=. uv run --no-sync python -c "
from warden.tests.observability.audit.test_smoke import validate_jsonl
errs = validate_jsonl('warden/observability/audit/logs/oh-smoke-1.jsonl', {
    'event_types': ['PreToolUse', 'PostToolUse'],
    'min_events': 2,
})
print('PASS' if not errs else errs)
"
```

---

## Layer 4: Cross-Provider Aggregation

Verify that `aggregate.py` can read JSONL from all providers together.

### Dry run (print counts)

```bash
PYTHONPATH=. uv run --no-sync python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --dry-run
```

**Pass criteria:**
- [ ] Events loaded > 0
- [ ] Runs include both `smoke-*` (Claude SDK) and `oh-smoke-*` (OpenHarness)
- [ ] No parse errors or schema mismatches
- [ ] Tool Usage Matrix shows tools from both providers

### Generate report

```bash
PYTHONPATH=. uv run --no-sync python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --output /tmp/audit-report.md
cat /tmp/audit-report.md
```

**Pass criteria:**
- [ ] Report generates without errors
- [ ] Tool Usage Matrix includes both Claude tools (`Read`, `Write`, `Bash`) and OH tools (`read_file`, `write_file`, `bash`)

---

## Layer 5: The live audit-trail gate (all three providers)

The reproducible M5 acceptance gate. Fires an audited turn per provider using a
**config-first** `AuditConfig` (not a raw env read), validates the per-agent
JSONL trails, runs the `aggregate` + `derive_manifest` derivation, and asserts
the governance stop lands (AUD-3). Host-run: OAuth Claude + OAuth Codex + free
Ollama qwen3:8b.

```bash
warden/docker/run.sh --audit-trail            # all three
warden/docker/run.sh --audit-trail claude     # one provider (claude|openharness|codex|all)
```

Under the hood it runs `warden.tests.e2e.audit_trail_smoke` — read that
file for the exact config-first construction to mirror in your own code.

---

## Claude SDK Smoke Tests (reference)

The Claude SDK audit smoke tests run via the CLI, validated with
`warden/tests/observability/audit/test_smoke.py`:

```bash
# Run a Claude audit smoke (AUDIT_* env populates AuditConfig via the settings layer)
AUDIT_ENABLED=1 AUDIT_RUN_ID=smoke-verify PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --single "Read CLAUDE.md and tell me the project name"

# Validate
PYTHONPATH=. uv run --no-sync python -c "
from warden.tests.observability.audit.test_smoke import validate_jsonl
errs = validate_jsonl('warden/observability/audit/logs/smoke-verify.jsonl', {'event_types': ['PreToolUse', 'PostToolUse'], 'min_events': 2})
print('PASS' if not errs else errs)
"
```

---

## What each layer validates

| Layer | Ollama needed | Network needed | What it catches |
|-------|:---:|:---:|---|
| 1. Unit tests | No | No | Event mapping bugs, JSONL format regressions, schema changes, derivation/record logic |
| 2. Hook registration | No | No | Import errors, API mismatches, missing dependencies |
| 3. Smoke tests | Yes | No | End-to-end flow: session → hook → subprocess → JSONL |
| 4. Cross-provider aggregation | No | No | Format divergence between providers, parse errors |
| 5. Live audit-trail gate | Yes | provider auth | Full config-first path across all 3 providers + derivation + AUD-3 |

Run layers 1–2 after every code change. Run layers 3–4 before committing audit-related changes. Run layer 5 as the final acceptance check.

---

## Troubleshooting

### Smoke test produces empty JSONL

1. Check the audit config is enabled — `AUDIT_ENABLED=1` (populates `AuditConfig.enabled`)
2. Check `PYTHONPATH=.` is set (the OpenHarness handler subprocess needs it to find the `audit` module)
3. Check `python` vs `python3` — the OH handler uses `sys.executable` from the parent process. If running via a different Python than the workspace venv, the handler subprocess may fail silently.

### OpenHarness handler subprocess fails silently

The handler catches all exceptions and exits 0 (audit never blocks). To debug it in isolation — this is exactly the `config → env → child` boundary, so you pass `AUDIT_*` as env:

```bash
OPENHARNESS_HOOK_PAYLOAD='{"event":"pre_tool_use","tool_name":"bash","tool_input":{"command":"echo hi"}}' \
AUDIT_RUN_ID=debug AUDIT_LOG_DIR=/tmp/audit-debug \
uv run --no-sync python -m warden.observability.audit.openharness_hook_handler

cat /tmp/audit-debug/debug.jsonl
```

If no output, run with logging:

```bash
OPENHARNESS_HOOK_PAYLOAD='{"event":"pre_tool_use","tool_name":"bash","tool_input":{}}' \
AUDIT_RUN_ID=debug AUDIT_LOG_DIR=/tmp/audit-debug \
uv run --no-sync python -c "
import logging; logging.basicConfig(level=logging.DEBUG)
from warden.observability.audit.openharness_hook_handler import main
main()
"
```

### Aggregation misses events

Check that all providers use the same `event_type` values:
```bash
PYTHONPATH=. uv run --no-sync python -c "
import json
with open('warden/observability/audit/logs/oh-smoke-1.jsonl') as f:
    for line in f:
        e = json.loads(line)
        print(e['event_type'], e.get('gen_ai.operation.name'))
"
```

Expected: `PreToolUse execute_tool`, `PostToolUse execute_tool`, `Stop stop` — the same strings all three providers emit.

### Codex tool calls invisible in the trail

If codex runs a tool but no `PreToolUse`/`PostToolUse` line appears: the SDK wraps
each stream item in a pydantic `RootModel` (`payload.item.root` →
`CommandExecutionThreadItem`), so the item must be unwrapped before the tap reads
its kind/fields — a recently-fixed bug that had made codex tool calls invisible.
Also note codex exec is **fail-closed**: a tool only appears in the trail if it
actually executed (an approved command-execution). See
[provider-audit-mechanisms.md](provider-audit-mechanisms.md).
