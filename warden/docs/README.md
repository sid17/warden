# Warden — Documentation

Warden (the `warden/` package) is a **tenant-isolated, provider-agnostic execution
engine for running AI systems**. It gives an application one contract for driving an agent —
across multiple model providers — with isolation, authentication, permissioning, cost
governance, audit, and observability built in as *mechanism*, so the application only supplies
*policy*. (The Python package is `warden`; "the harness" is used throughout the docs as a
synonym for the runtime.)

New here? Read **[01 — Design Concept Note](01-conceptual-model.md)** first: it explains what
the harness is, its layers, and the seams an app plugs into. **Integrating the harness into a
product?** Start with **[Product Integration](product_integration.md)** — the two integration
shapes (long-horizon tool-calling jobs vs. real-time streaming) and the seams/steps for each.

## Core concepts (read in order)

| # | Doc | What it covers |
|---|-----|----------------|
| 01 | [Design Concept Note](01-conceptual-model.md) | What the harness **is** — principle (mechanism vs. policy), layers, workspaces, sessions, seams, drive paths |
| 02 | [Observability](02-observability.md) | Telemetry model — traces, spans, metrics; OTel + Langfuse |
| 03 | [Safety Model](03-safety.md) | Input/output guardrails, sanitization, experiment presets |
| 04 | [Audit](04-audit.md) | The audit trail — what each provider records and why |
| 05 | [App Interaction Patterns](05-app-interaction-patterns.md) | How an app builds prompt-level behavior on top of the harness |
| 06 | [Resource Governance](06-resource-governance.md) | Cost governance — the Governor, ledgers, spend ladder |
| 07 | [Provider Contract & Testing](07-provider-contract-and-testing.md) | The `AgentProvider` contract + end-to-end Docker testing |
| 08 | [Session Contract & Testing](08-session-contract-and-testing.md) | Session lifecycle, resume semantics, and how sessions are tested |
| 09 | [Environment & Credentials](09-environment-and-credentials.md) | Setting up auth & credentials the typed, secrets-by-reference way |
| 10 | [Adding a Profile](10-adding-a-profile.md) | How to add a new application profile to the harness API |
| 11 | [Permissions & Human-in-the-Loop](11-permissions-and-human-in-the-loop.md) | The permission seam — verdict shape, the gate chain, tool scope & custom tools, and the two HITL shapes (warm hold vs. durable HTTP pause/resume) |
| 12 | [The Runs API](12-the-runs-api.md) | Running the harness as an HTTP service — run lifecycle, routes, events & egress, service-token auth, local vs. distributed state backends |

## Guides

Task-oriented runbooks — follow one to *do* something. See **[guides/README.md](guides/README.md)** for the full index (audit & safety, permissions, observability).

## Providers

Reference-implementation design notes for each concrete provider:

| Provider | Doc |
|----------|-----|
| Claude SDK | [providers/claude-sdk.md](providers/claude-sdk.md) |
| Codex Python SDK | [providers/codex-sdk.md](providers/codex-sdk.md) |
| OpenHarness | [providers/openharness.md](providers/openharness.md) |

## Reference (architecture deep-dives)

Longer-form architecture notes for readers extending the engine. See **[reference/README.md](reference/README.md)**:
orchestration layer, provider auth & home isolation, audit-hook architecture, telemetry architecture, safety architecture.

## Integrating a product

To drive the harness from a product, start with **[product_integration.md](product_integration.md)**
(the two integration shapes) and **[10-adding-a-profile.md](10-adding-a-profile.md)** (the profile
seam). Product-specific worked examples live with the product, not in this package.
