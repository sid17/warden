# Audit-to-Config Process — Deriving Safety Configurations

> How to turn an audit trail into per-sub-agent least-privilege configs. This is
> now **automated** by `derive_manifest.py` — this guide points you at the tool
> first, then explains what it does under the hood so you can read and adjust its
> proposal.
>
> **Start here?** If you haven't run an audit yet, start with [cli-and-audit.md](cli-and-audit.md) to generate logs first.

---

## Prerequisites

- Audit JSONL logs at `warden/observability/audit/logs/` (or your `$AUDIT_LOG_DIR`), ideally from **2+ runs** of the same workflow so convergence is measurable
- Optionally, an audit report from `aggregate.py` for a human-readable view (see the [audit README](../../../observability/audit/README.md) for what each section means)

## The primary path — `derive_manifest.py` (automated)

`derive_manifest.py` reads the raw audit logs (reusing `aggregate.py`'s builders)
and **proposes** a per-sub-agent least-privilege manifest diff. It never
auto-applies — it writes a JSON proposal for you to review.

```bash
PYTHONPATH=. uv run --no-sync python warden/observability/audit/derive_manifest.py \
  warden/observability/audit/logs/ \
  --output warden/observability/audit/reports/my-workspace/manifest-diff.json \
  --threshold 0.8
```

For each sub-agent that has **converged** (tool usage stable ≥ the threshold,
default `0.8`, across ≥2 runs), it proposes:

| Field | Derived from | Meaning |
|-------|--------------|---------|
| `disallowed_tools` | Tool Usage Matrix | Tools in the active universe the agent never used |
| `read_globs` / `write_globs` | Path Access Map | Directory globs (`{parent}/**`) the agent read/wrote |
| `bash_rules.allow_commands` | Command Inventory | Bash commands the agent actually ran |
| `pretool_path_rules` | Path Access Map | A `PreToolUse` path rule (composes with M4's SAFE-6 hook) restricting `Read/Write/Edit/MultiEdit` to observed globs, `on_violation: deny` |

Two agents are deliberately **kept broad** (not locked down):

- The **root orchestrator** — safety comes from scoping the sub-agents it spawns, not from locking the orchestrator itself.
- **Not-converged agents** — single-run agents (convergence not measurable) or agents with stability `< 0.8` (tool usage still varying). These are not ready to lock down; widen, keep auditing, re-run.

The output is a proposed diff; apply it deliberately to your agent definitions.

## What it does under the hood (the manual reasoning)

The tool automates the mental model below; read this to understand and sanity-check its proposal.

### Read the audit trail

| Section | What to look for |
|---------|-----------------|
| Tool Usage Matrix | Which agents use which tools — unused tools become `disallowed_tools` |
| Path Access Map | Which agents read/write which paths — becomes `read_globs`/`write_globs` + the PreToolUse rule |
| Command Inventory | What Bash commands agents run — becomes `bash_rules.allow_commands` |
| Convergence | Stability across runs — `<80%` means behavior varies (keep broad) |

### `disallowed_tools`

Tools the agent **never used** in any run. Safe to disallow.

```python
# Agent only used Read and Write → disallow everything else
disallowed_tools=["Agent", "Bash", "Edit", "Glob", "Grep"]
```

### `hooks` (PreToolUse path enforcement)

Based on the Path Access Map — restrict writes to observed directories. This is
exactly the `pretool_path_rules` entry the tool proposes, expressed as a hook:

```python
async def path_hook(hook_input, tool_use_id, context):
    if hook_input["tool_name"] in ("Write", "Edit"):
        path = hook_input["tool_input"].get("file_path", "")
        if not path.startswith("/expected/output/dir/"):
            return {"decision": "block", "reason": f"Write outside allowed dir: {path}"}
    return {}
```

### Low convergence agents

Agents with `<80%` stability: widen permissions, keep audit hooks on for
monitoring. Don't restrict until more runs confirm stable behavior — this is
what `derive_manifest.py` does automatically (leaves them broad).

## Apply and wire the config

### Option A — CLI flags (quick testing)

```bash
PYTHONPATH=. uv run --no-sync python -m warden.drive.cli \
  --denied-tools "Agent,Bash,Edit,Glob,Grep" \
  --single "your pipeline prompt"
```

### Option B — programmatic construction (production)

Keep auditing active while adding restrictions. `build_audit_hooks()` now takes
`run_id=`/`log_dir=` (closurized at build time, no env at fire):

```python
from claude_agent_sdk import ClaudeAgentOptions
from warden.observability.audit.claude_sdk_hooks import build_audit_hooks

options = ClaudeAgentOptions(
    cwd="/path/to/repo",
    disallowed_tools=["Agent", "Bash", "Edit", "Glob", "Grep"],
    hooks={
        **build_audit_hooks(run_id="restricted-1", log_dir="/tmp/audit-logs"),
        "PreToolUse": [path_hook],  # add path enforcement
    },
)
```

Write your recommendations (and the reviewed `manifest-diff.json`) to
`warden/observability/audit/reports/{workspace-name}/`.

## Test the restricted pipeline

Run the pipeline again with restrictions and compare:

```bash
# Run with restrictions + audit
AUDIT_ENABLED=1 AUDIT_RUN_ID=restricted-1 PYTHONPATH=. uv run --no-sync python -m warden.drive.cli \
  --denied-tools "Agent,Bash,Edit,Glob,Grep" \
  --single "your pipeline prompt"

# Compare output to unrestricted run
diff /tmp/unrestricted-output/ /tmp/restricted-output/

# Check for blocked actions in the JSONL
grep '"decision":"block"' warden/observability/audit/logs/restricted-1.jsonl
```

**Success criteria:**
- Pipeline completes without errors
- Output quality matches unrestricted runs
- No false positive blocks on legitimate actions
- Audit report for restricted run shows same tool patterns (no new tools needed)

---

## Quick Reference

| Step | What | Output |
|------|------|--------|
| Run audit | [cli-and-audit.md](cli-and-audit.md) Steps 1-4 (2+ runs) | `logs/*.jsonl` |
| Derive (automated) | `derive_manifest.py logs/ --output …` | `reports/{workspace-name}/manifest-diff.json` (PROPOSED) |
| Review | Confirm disallowed tools, path globs, bash rules; root + `<80%` left broad | Reviewed diff |
| Apply | Wire into agent defs / CLI flags | Restricted config |
| Test | Run pipeline with restrictions, diff output | Validation that nothing broke |
