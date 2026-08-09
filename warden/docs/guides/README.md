# Guides

Operational runbooks — follow these to do things with the orchestrator.

## Audit & Safety

| Guide | What it helps you do |
|-------|---------------------|
| [cli-and-audit.md](audit/cli-and-audit.md) | **Start here** — run the CLI, audit a workspace or workflow, generate a report |
| [audit-process.md](audit/audit-process.md) | Translate an audit report into per-agent SDK configs (disallowed_tools, PreToolUse hooks) |
| [testing-audit.md](audit/testing-audit.md) | Verify audit hooks work — unit tests, smoke tests, JSONL validation |
| [production-readiness.md](audit/production-readiness.md) | Deployment checklist for safety guardrails (PreToolUse hooks, per-agent scoping, output sanitization) |

## Permissions

| Guide | What it helps you do |
|-------|---------------------|
| [tool-permission-gating.md](permissions/tool-permission-gating.md) | Understand + prove which tools the `can_use_tool` gate actually honors (regular vs custom, per provider) — and why custom-tool HITL only fires on OpenHarness |

## Observability

| Guide | What it helps you do |
|-------|---------------------|
| [testing-instrumentation.md](observability/testing-instrumentation.md) | Verify OTel and Langfuse telemetry after code changes |
| [langfuse-smoke-test.md](observability/langfuse-smoke-test.md) | Validate Langfuse trace hierarchy, sub-agent nesting, metadata |
| [otel-smoke-test.md](observability/otel-smoke-test.md) | Validate OTel spans reach Tempo and metrics reach Prometheus |
