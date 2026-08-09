# Audit Guides

How to audit agent behavior, derive safety configs, and verify hooks.

| Guide | When to use |
|-------|-------------|
| [cli-and-audit.md](cli-and-audit.md) | **Start here** — run the CLI, audit a workspace or workflow, generate a report (config-first) |
| [provider-audit-mechanisms.md](provider-audit-mechanisms.md) | How audit works differently across the three providers (Claude / OpenHarness / Codex) — same schema, different mechanisms + completeness |
| [audit-process.md](audit-process.md) | Derive per-sub-agent least-privilege configs — now automated via `derive_manifest.py` |
| [testing-audit.md](testing-audit.md) | Verify audit hooks work after code changes (5-layer test plan) |
| [production-readiness.md](production-readiness.md) | Future work checklist for deploying safety configs in production |

For how the audit system works internally (hooks, JSONL schema, structure), see the [audit README](../../../observability/audit/README.md).
