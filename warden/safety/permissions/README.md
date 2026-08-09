# Permissions — Workflow Rule Enforcement

Production permission evaluation. Runs in every chat session — the orchestrator calls `PermissionChecker` before every tool execution via the `can_use_tool` callback.

## How It Works

```
Agent wants to call Write("src/main.py", ...)
  → Orchestrator's can_use_tool callback fires
  → PermissionChecker.evaluate()
    → Check workflow YAML tool access rules (allowed/denied tools)
    → Check file access globs (read/write patterns)
    → Check sensitive paths (.env, credentials, private keys)
  → Returns PermissionDecision (allow / deny / requires_confirmation)
```

## Files

| File | Purpose |
|------|---------|
| `checker.py` | `PermissionChecker` — evaluates tool + file access against workflow YAML rules. `PermissionDecision` dataclass. `PermissionMode` enum (confirm, read_only, auto). |
| `sensitive_paths.py` | Definitions of sensitive paths and patterns — `.env`, `credentials.json`, `*.pem`, `~/.ssh/` — that trigger confirmation prompts regardless of workflow config |

## Workflow YAML Integration

Permission rules come from the workflow's `permissions:` section:

```yaml
permissions:
  mode: default           # confirm | read_only | auto
  file_access:
    read: ["**/*"]         # glob patterns
    write: ["courses/**"]  # restrict writes to output dir
  tool_access:
    allowed: [Read, Write, Grep, Glob, Bash]
    denied: [Agent]        # no sub-agent spawning
```

The checker loads these at session creation time and evaluates every tool call against them.
