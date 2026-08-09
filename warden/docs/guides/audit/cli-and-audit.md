# CLI & Audit Guide

> Entry point for running the harness CLI and auditing any workspace or workflow.

## Config-first mental model (read this first)

Env enters the harness in **exactly one place** — the pydantic-settings layer.
It reads `AUDIT_ENABLED` / `AUDIT_RUN_ID` / `AUDIT_LOG_DIR` into the typed
`HarnessConfig.observability.audit` (`AuditConfig(enabled, run_id, log_dir)`).
From there the **config object** is threaded through the call graph
(ChatAPI → Orchestrator → sessions); no harness code reads `os.environ` for its
own decisions. So `config.observability.audit` is the single source of truth —
`AUDIT_ENABLED=1` on the CLI or in `.env` still works, but only because it
**populates that config** via the settings layer. It is never read directly.

The one exception is a subprocess boundary: OpenHarness audit hooks are child
processes, so the session **derives** `AUDIT_*` env *from* config and injects it
(`config → env → child`). That is serialization across a process boundary, not
ambient env access. See [provider-audit-mechanisms.md](provider-audit-mechanisms.md).

## Running the CLI

All commands run from the **repo root** (the uv workspace root).

### Single-shot prompt

```bash
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --single "Your prompt here"
```

### Interactive REPL

```bash
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli
```

### With a specific provider or model

```bash
# Use OpenHarness provider
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --provider openharness --single "prompt"

# Override model
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --model claude-sonnet-4-20250514 --single "prompt"
```

### With tool restrictions

```bash
# Only allow specific tools
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --allowed-tools "Read,Grep,Glob" --single "prompt"

# Deny specific tools
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --denied-tools "Bash,Write" --single "prompt"
```

---

## Auditing a Workspace or Workflow

The audit answers: which tools were called, on which files, by which agent — and should we restrict that? Use it whenever you add a new workspace or workflow and want to know what safety config it needs.

Audit is controlled by `config.observability.audit`. The `AUDIT_*` env vars below
**populate** that config through the settings layer (they still work) — think of
them as the CLI/`.env` way of setting `AuditConfig(enabled=True, …)`. All three
providers (Claude, OpenHarness, Codex) write the **same** `AuditEvent` JSONL
schema, but obtain events by different mechanisms with different completeness —
see [provider-audit-mechanisms.md](provider-audit-mechanisms.md).

### Step 1: Run with auditing enabled (2-3 times)

```bash
# Run 1 — Claude provider (default)
AUDIT_ENABLED=1 AUDIT_RUN_ID=run-1 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --single "your pipeline prompt"

# Run 2 — same prompt, fresh run (for convergence analysis)
AUDIT_ENABLED=1 AUDIT_RUN_ID=run-2 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --single "your pipeline prompt"

# With a workflow
AUDIT_ENABLED=1 AUDIT_RUN_ID=run-1 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --workflow my-workflow --single "your prompt"

# With OpenHarness provider
AUDIT_ENABLED=1 AUDIT_RUN_ID=run-1 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli --provider openharness --single "your prompt"
```

Each run creates `warden/observability/audit/logs/{run-id}.jsonl`
(or `$AUDIT_LOG_DIR/{run-id}.jsonl` when `AUDIT_LOG_DIR` is set). More runs =
better convergence analysis.

Create a folder for this audit and record what you ran:

```bash
mkdir -p warden/observability/audit/reports/my-workspace/
```

Record what you ran and why in `warden/observability/audit/reports/my-workspace/run-manifest.md`.

#### Programmatic equivalent (config-first)

The env recipe above is a convenience wrapper over the real API — a typed
`AuditConfig` threaded through `HarnessConfig`. This is how the live gate
(`tests/e2e/audit_trail_smoke.py`) does it:

```python
from warden import ChatAPI, HarnessConfig
from warden.config.models import AuditConfig

cfg = HarnessConfig()
cfg.provider.provider = "claude"
cfg.observability.audit = AuditConfig(
    enabled=True,
    run_id="run-1",
    log_dir="/tmp/audit-logs",   # None → default logs/ dir
)

api = ChatAPI(cfg, repo_path=".")
await api.init()
async for ev in api.send("your pipeline prompt", workflow=None):
    ...
await api.close()
```

### Step 2: Quick look at what happened

```bash
# Count events
wc -l warden/observability/audit/logs/run-1.jsonl

# See event types
PYTHONPATH=. uv run --no-sync python -c "
import json
with open('warden/observability/audit/logs/run-1.jsonl') as f:
    for line in f:
        e = json.loads(line)
        tool = e.get('tool_name', '')
        agent = e.get('agent_id', '(main)')
        print(f\"{e['event_type']:20s} tool={tool:10s} agent={agent}\")
"
```

### Step 3: Generate a full report

```bash
# Dry run — just print counts
PYTHONPATH=. uv run --no-sync python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --dry-run

# Generate markdown report
PYTHONPATH=. uv run --no-sync python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --output warden/observability/audit/reports/my-workspace/audit-report.md
```

The report shows:
- **Tool Usage Matrix** — which agents used which tools, how many times
- **Path Access Map** — which files each agent read/wrote
- **Command Inventory** — what Bash commands were run
- **Convergence** — how stable tool usage is across multiple runs

### Step 4: Scan output files for leaked internals (optional)

```bash
PYTHONPATH=. uv run --no-sync python warden/observability/audit/scan_output.py /path/to/output/
```

Detects system prompt fragments, internal paths, skill/agent references in generated files.

### Step 5: Use audit data to set permissions

After auditing, you know exactly what each agent needs. The derivation is now
**automated** — `derive_manifest.py` reads the same logs and proposes a
per-sub-agent least-privilege diff (see [audit-process.md](audit-process.md)).
To apply a proposed restriction quickly at the CLI:

```bash
# Agent only used Read and Write? Deny everything else:
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli \
  --denied-tools "Agent,Bash,Edit,Glob,Grep" \
  --single "Your prompt here"

# Run again with restrictions + audit to verify it still works:
AUDIT_ENABLED=1 AUDIT_RUN_ID=restricted-1 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli \
  --denied-tools "Agent,Bash,Edit,Glob,Grep" \
  --single "Your prompt here"

# Compare outputs to confirm nothing broke
```

**Success criteria:**
- Pipeline completes without errors
- Output quality matches unrestricted runs
- No false positive blocks on legitimate actions

### Reproducible end-to-end check

The live gate fires an audited turn per provider (config-first `AuditConfig`),
validates the JSONL trails, runs the derivation, and asserts the governance stop
lands (AUD-3):

```bash
warden/docker/run.sh --audit-trail        # all three providers
warden/docker/run.sh --audit-trail claude # one provider
```

---

## What's Next

- For **how the audit system works internally** (hooks, JSONL schema, provider mechanisms): see the [audit README](../../../observability/audit/README.md)
- For **how audit differs across the three providers** (Claude / OpenHarness / Codex): see [provider-audit-mechanisms.md](provider-audit-mechanisms.md)
- For **deriving per-agent configs** (now automated via `derive_manifest.py`): see [audit-process.md](audit-process.md)
- For **testing audit hooks after code changes**: see [testing-audit.md](testing-audit.md)
- For **production deployment checklist**: see [production-readiness.md](production-readiness.md)

---

## Provider-Specific Notes

Short version below; full cross-provider treatment (with the completeness matrix)
lives in [provider-audit-mechanisms.md](provider-audit-mechanisms.md).

### Claude SDK Audit Hooks
- Uses async Python callbacks via `ClaudeAgentOptions.hooks`
- Hooks run in-process — no subprocess overhead; `run_id`/`log_dir` are closurized at build time (zero env at fire)
- Implementation: `warden/observability/audit/claude_sdk_hooks.py`

### OpenHarness Audit Hooks
- Uses the native hook system (`HookRegistry` + `HookExecutor`)
- Hooks run as `command` type — each event spawns a subprocess that writes JSONL
- The subprocess reads config via `AUDIT_RUN_ID`/`AUDIT_LOG_DIR` env (the `config → env → child` boundary) and the payload via `$OPENHARNESS_HOOK_PAYLOAD`
- Implementation: `warden/observability/audit/openharness_hooks.py` (registry), `warden/observability/audit/openharness_hook_handler.py` (handler)

### Codex Audit Tap
- Codex has **no** native hook system — the trail is **derived** from the event stream by `CodexAuditTap` (`tool_use`→`PreToolUse`, `tool_result`→`PostToolUse`)
- Command-execution tool calls only; no lifecycle events, no MCP custom tools
- Implementation: `warden/providers/codex/audit_tap.py`

All three providers produce identical JSONL format — aggregation, reports, and
`derive_manifest.py` work across all three without changes.
