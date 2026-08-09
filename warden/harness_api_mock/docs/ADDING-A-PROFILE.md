# Adding a product profile to the mock harness

The mock harness is a **product-agnostic** Runs-API engine. Everything specific to one
product lives in a **profile**. One mock process serves one product; the active profile is
chosen at boot by `MOCK_WARDEN_PROFILE` (default `example`, the product-agnostic in-tree
profile). Adding a product's mock means dropping a `<name>/profile.py` exposing `PROFILE` —
**no engine file changes, not even a registry line** (the loader imports it by convention).

`profiles/example/` is the minimal in-tree reference. A real product profile lives
**out-of-tree in the product's own package** and is loaded by a fully-qualified, dotted
`MOCK_WARDEN_PROFILE` (e.g. `yourco_integration.myproduct.mock`) — a bare name resolves
under this built-in `profiles/` package, a dotted name is imported verbatim. See the
top-level [`../../docs/10-adding-a-profile.md`](../../docs/10-adding-a-profile.md) and
[`../../docs/product_integration.md`](../../docs/product_integration.md) for the full seam.

> Paths below are relative to the package root, `warden/harness_api_mock/`
> (this doc lives one level down, in `docs/`).

## The engine vs. the profile

| Engine core (never edit to add a product) | Profile (`profiles/<name>/`) |
|---|---|
| `app.py` routes, `runner.py` step-player, gate/SLA, sessions | `scripts.py` — the canned `Step` sequences |
| `steps.py` — the `Step` vocabulary (`EmitStep`/`SleepStep`/`InvokeToolStep`/`GateStep`) | `invoker.py` — the real writeback bridge |
| `files.py` fixture store + path guard, `event_log.py`, `contract.py` | `fixtures/` — artifact bytes served by `GET /file` |
| `tool_seam.py` — the `ToolInvoker` **Protocol** + `NoopToolInvoker` | `profile.py` — binds the three into a `PROFILE` |
| `profile_loader.py` — the `Profile` contract + convention loader | |

## The `Profile` contract

`profiles/<name>/profile.py` must expose a module-level `PROFILE: Profile`
(`profile_loader.Profile`):

```python
from pathlib import Path
from warden.harness_api_mock.profile_loader import Profile
from warden.harness_api_mock.profiles.myapp.invoker import MyAppToolInvoker
from warden.harness_api_mock.profiles.myapp.scripts import SCRIPTS

def _build_invoker(config, job_id_for):
    # config: MockConfig (read config.product_api_url / product_api_token / tools_apply / …)
    # job_id_for: run_id -> task_id resolver, supplied by the runner's registry (D7)
    return MyAppToolInvoker(job_id_for=job_id_for, api_base_url=config.product_api_url, ...)

PROFILE = Profile(
    name="myapp",
    scripts=SCRIPTS,                              # dict[str, Script]; MUST include "default"
    fixture_dir=Path(__file__).parent / "fixtures",
    build_invoker=_build_invoker,                 # (MockConfig, job_id_for) -> ToolInvoker
)
```

## The four steps

1. **`profiles/<name>/scripts.py`** — define `SCRIPTS: dict[str, Script]` mapping each
   `input.workflow` your product sends to an ordered `list[Step]`. Import the step types
   from the engine (`from warden.harness_api_mock.steps import EmitStep, …`).
   Invariants: first event is `session`; exactly one terminal (`result`/`error`/`stopped`);
   the runner assigns `seq`. Use `GateStep` for a durable-HITL pause, `InvokeToolStep` to
   fire a writeback tool (invisible on the wire). Include a `"default"` key.

2. **`profiles/<name>/invoker.py`** — implement the `ToolInvoker` Protocol
   (`invoke(run_id, tool_name, args)` + `confirm_landscape(run_id, concepts)`). This is
   the one place that touches product code — **lazy-import** your product's tools *inside*
   the methods so importing the profile stays product-free until a tool fires. Not building
   real writeback yet? Skip it and run with `MOCK_WARDEN_TOOL_INVOKER_MODE=noop`.

3. **`profiles/<name>/fixtures/`** — one subdir per workflow that writes files (e.g.
   `<workflow>/courses/<slug>/*.md`). Seeded into each run's workspace at submit; served by
   `GET /file`. A `result`-only script (e.g. read-only Q&A) needs no fixtures.

4. **`profiles/<name>/profile.py`** — the `PROFILE` above. Add `__init__.py` files for the
   package.

## Running it

```bash
MOCK_WARDEN_PROFILE=myapp \
MOCK_WARDEN_TOOL_INVOKER_MODE=profile \        # or "noop" for a product-free run
MOCK_WARDEN_PRODUCT_API_URL=http://myapp-api:PORT/api/myapp/v1 \
MOCK_WARDEN_PRODUCT_API_TOKEN=<writeback JWT> \
python -m warden.harness_api_mock.server
```

The loader resolves `profiles/myapp/profile.py`; an unknown `MOCK_WARDEN_PROFILE` fails
loud with a `ValueError` (LAW 4). That's the whole extension surface — the engine never
learns your product's name in code.

## Config knobs (generic)

`MockConfig` (`config.py`) carries only product-agnostic fields: `profile`,
`tool_invoker_mode` (`noop|profile`), `product_api_url` / `product_api_token` /
`product_tools_log`, `tools_apply`, `fixture_dir` (override; empty → the profile's own),
`step_delay_s`, `sla_seconds`, plus the fault-injection knobs (`inject_error_at`,
`budget_stop`). All env-overridable (`MOCK_WARDEN_*`).
