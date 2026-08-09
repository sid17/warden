# Reference

Architecture and design documents — study material for understanding how the orchestrator works, why it's built this way, and the landscape of tools it integrates with.

## System Design

| Document | Topic |
|----------|-------|
| [orchestration-layer.md](orchestration-layer.md) | How the transport-agnostic orchestrator works — middleware, tool scoping, permissions, session lifecycle, streaming |
| [safety-architecture.md](safety-architecture.md) | Layered safety stack — system prompts, tool restriction, input/output filtering, intent classification |
| [audit-hook-architecture.md](audit-hook-architecture.md) | JSONL audit logging via provider hooks — Claude SDK callbacks vs OpenHarness subprocess hooks |
| [telemetry-architecture.md](telemetry-architecture.md) | OTel + Langfuse pipeline — native SDK instrumentation, collector routing, dashboards |
| [provider-auth-and-home-isolation.md](provider-auth-and-home-isolation.md) | How auth works across the four providers, and why pinning a task's home (persistence) strands the login unless a token is injected via env |

