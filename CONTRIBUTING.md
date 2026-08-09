# Contributing to Warden

Thanks for your interest in Warden — a tenant-isolated, provider-agnostic agent runtime.

## Development setup

Warden uses [uv](https://docs.astral.sh/uv/) for dependency management (Python 3.11–3.14).

```bash
git clone <your-fork-url>
cd warden
uv sync --extra postgres --extra telemetry   # dev deps + optional extras
uv run pytest -q                              # full hermetic suite
```

The test suite is **hermetic** — it needs no credentials, network, or running services.
Tests marked `slow` / `ollama` and the Postgres/live-provider suites are deselected by
default (they require external infrastructure).

## Ground rules

- **Mechanism vs. policy.** Warden is *mechanism*. It owns the verbs (run, stream,
  isolate, enforce, persist, resume); the application owns the nouns (which prompt, which
  decision, who pays). Product-specific coupling belongs in a **profile** (see
  [`warden/docs/10-adding-a-profile.md`](warden/docs/10-adding-a-profile.md)), never in the
  engine. `grep -r <your-product> warden/` must return nothing.
- **Fail closed.** Permissioning, isolation, and safety must default to denial. No silent
  failures — log or raise, never swallow.
- **Tests alongside code.** New behavior ships with tests in the same change. Bug fixes
  start with a failing test.
- **Keep it typed.** Don't widen types or disable a lint to dodge an error — fix the cause.

## Before you open a PR

```bash
uv run --with ruff ruff check warden/     # lint
uv run pytest -q                          # tests green
```

- Keep changes focused; one concern per PR.
- Describe what changed and why. Link any related issue.
- By contributing, you agree your contributions are licensed under the project's
  [Apache-2.0](LICENSE) license.

## Architecture orientation

Start with [`warden/docs/01-conceptual-model.md`](warden/docs/01-conceptual-model.md)
(what Warden *is* — layers, seams, mechanism vs. policy) and
[`warden/docs/README.md`](warden/docs/README.md) (the numbered chapters).
