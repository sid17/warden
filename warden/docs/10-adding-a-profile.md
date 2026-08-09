# 10 — Adding a Profile

> New to integrating the harness? Read [`product_integration.md`](./product_integration.md)
> first — it frames the two integration shapes and where this profile seam fits. This doc is
> the how-to for the tool/writeback ("long-horizon job") shape.

A **profile** is how a *product* teaches the generic harness to run *its* skill —
without the harness core ever learning a product noun. It is the single, sanctioned
seam for product-specific coupling. This doc is the playbook for adding one.

If you only remember one thing: **the harness core is product-agnostic; every
product-specific thing lives in a profile (harness-side adapter) plus the product's own
seed (product-side data). The core depends on neither — it speaks only generic
contracts.** A profile is where you make a generic server drive a specific product.

**Where a profile lives.** A profile is loaded by name at boot (`WARDEN_PROFILE` for the
real server, `MOCK_WARDEN_PROFILE` for the mock). A **bare** name resolves against the
engine's built-in `harness_api/profiles/<name>/` package — where the shipped, product-agnostic
**`example`** profile lives (the in-tree reference). A **dotted** name is a fully-qualified
module path imported *verbatim*, so a real product profile lives **out-of-tree in the product's
own package** (e.g. `yourco_integration.<product>.real`) and the open-source engine never
contains it. This doc describes the general pattern; the mock `example` profile
(`harness_api_mock/profiles/example/`) is the minimal in-tree reference.

## What a profile provides

Exactly two things, both per-run:

1. **Custom tools** — the product's in-process MCP tools (a `list[CustomTool]`), injected
   into each run's config so the model can call them.
2. **A seeded workspace** — the skill's files on disk (skills, agents, read-docs) plus the
   empty write-dirs it expects, derived from the product's source of truth.

Everything else (auth, streaming, governance, telemetry, provider selection, the Runs API
surface) is the generic core and needs no per-product change.

## The invariant

- The core (`harness_api/app.py`, `runner.py`, `workspace/`, `drive/`, `orchestrator/`, …)
  **names no product**. It consumes only generic contracts: `CustomTool`, the seed
  metadata, the copy/mkdir lists.
- A profile package (`harness_api/profiles/<name>/`) is the **only** place allowed to import product
  code, and it does so **lazily** (inside a function, never at module top level) — so
  loading a profile, or running with no profile, never pulls product code in.
- Product **data** (the seed manifest + the skill/agent/doc content) lives in the *product
  repo*, not here. The profile *reads* it; it doesn't vendored-copy it.

## The seams (exact symbols)

### 1. `WARDEN_PROFILE` → `harness_api/profiles/serve.py`

The server is launched via the profile-aware entrypoint `warden.harness_api.profiles.serve:app` instead of
the plain `harness_api.app:app`:

- `WARDEN_PROFILE` **unset** → returns the plain, product-agnostic app, byte-for-byte
  unchanged (product code never touched).
- `WARDEN_PROFILE=<name>` (bare) → imports `harness_api/profiles/<name>/profile.py`.
- `WARDEN_PROFILE=<a.b.c>` (dotted) → treated as a **fully-qualified module base** and
  imported verbatim as `<a.b.c>.profile` — this is how an **out-of-tree** product profile is
  loaded (e.g. `WARDEN_PROFILE=yourco_integration.myproduct.real`).

Either way, `serve.py` reads the module's `PROFILE`, builds its `chat_api_factory`, and
constructs the app around a `Runner(config, chat_api_factory=factory)`. This is the *only*
place a profile is applied — `harness_api/app.py` stays oblivious. (The mock loader,
`harness_api_mock/profile_loader.load_profile`, applies the identical bare-vs-dotted rule.)

### 2. `harness_api/profiles/<name>/profile.py` → `PROFILE`

A profile package exposes one module-level `PROFILE`:

```python
PROFILE = Profile(name="<name>", build_factory=build_<name>_factory)
```

`Profile` is a tiny local `@dataclass` — just `{name, build_factory}` where
`build_factory(cfg: HarnessApiConfig) -> ChatApiFactory` — defined in your profile package
(mirror the reference profile's `profile.py`).
Import the factory here; keep the product import inside the factory (lazy), so importing
`profile.py` alone stays product-free.

### 3. The per-run factory → inject the custom tools

`ChatApiFactory = Callable[[RunSpec, dict|None], object]` (see `runner.py`). The `Runner`
calls it once per run to build that run's `ChatAPI`. The generic default,
`runner._default_factory`, deep-copies the engine config and overlays per-run
provider/model/user/task/auth — **but never sets `custom_tools`**. Your factory mirrors
that body and adds the one injection line:

```python
def factory(spec: RunSpec, auth_env: dict | None) -> ChatAPI:
    build_custom_tools, ToolContext = _lazy_import_product()   # product import, lazy
    ctx = ToolContext(job_id=spec.task_id, ..., should_apply=..., log_path=...)
    config = cfg.engine.model_copy(deep=True)
    config.provider.provider = spec.provider
    config.provider.model = spec.model
    config.auth.auth_env = auth_env
    config.workspace.user_id = spec.user_id
    config.workspace.task_id = spec.task_id
    config.custom_tools.tools = build_custom_tools(ctx)         # <-- the EXT-P2 injection
    return ChatAPI(config, repo_path=config.workspace.base_dir,
                   workflow=spec.input.get("workflow"))
```

The tools are a `list[CustomTool]` (`warden/seams/custom_tools.py`) — a
`{name, description, input_schema, handler}` the provider surfaces as
`mcp__harness_custom__<name>`. **Set `config.custom_tools.tools` before `ChatAPI(...)`** —
`ChatAPI` reads `config.custom_tools` at construction. `job_id = spec.task_id` is the
run/job identity. (The factory currently duplicates `_default_factory`'s overlay body; if
a third consumer appears, extract a shared `_config_for_run(cfg, spec, auth_env)` helper.)

### 4. Workspace seeding → the generic seed contract

The core provisions a workspace from an opaque bundle; the product decides its contents.

- **Bundle shape.** A `.tar.gz` with a top-level `seed-meta.json` declaring the resolved
  contract: `workflows[]`, `copy: [{path, to}]` (content stored under `content/<path>`),
  and `mkdir: []` (empty write-dirs — they carry no bytes, so they travel as metadata, not
  tar members).
- **Flow.** `POST /seeds` (upload → content-addressed `seed_ref`) → `POST /provision
  {user_id, task_id, seed_ref}` → `workspace/provision.resolve_seed` reads the metadata and
  hands `workspace/bootstrap.bootstrap(...)` a generic **copy-list** + **mkdir-list** →
  `POST /runs {input.workflow=<name>}` (the run just names the workflow; `_assert_provisioned`
  requires it already on disk — **no `seed_ref` on the run**).
- **Guards.** `bootstrap._confined_dest` and `provision._confined_source` reject any
  absolute / `..` / NUL path so a malformed or hostile seed can never read or write outside
  the bundle/workspace. Keep these — they are the seed-trust boundary.
- **Building the bundle** is the profile's job: read the product's declarative manifest,
  **validate every source exists (fail loud)**, and pack `seed-meta.json` + `content/…` +
  `workflows/…`. The product manifest (an allowlist of what to copy + what to mkdir) is the
  *fact*; `bootstrap` is the *mechanism*; the manifest is the contract between them.

### 5. Bring-up → the profile's own `run.sh`

Product-specific docker wiring lives in **your profile package**, beside the profile (e.g.
`yourco_integration/<product>/real/run.sh`), **never** in the engine's generic
`docker/run-harness-api.sh` (no product knowledge in the shared bring-up, no product bed
gate). Your `run.sh` builds the generic engine image, then bind-mounts your integration
package + points `WARDEN_PROFILE` at it. The pattern:

- build the **generic** image, then `docker run` it with the profile's uvicorn target
  `warden.harness_api.profiles.serve:app` and `-e WARDEN_PROFILE=<a.b.c>` (the
  profile's fully-qualified module path);
- **bind-mount two things** read-only, since neither is baked into the open-source image:
  (a) the **out-of-tree profile package** into `/app` so it imports under the `engines`
  namespace — e.g. `-v <repo>/yourco_integration:/app/engines/yourco_integration:ro` (adjust
  to your package root); and (b) the **product backend** the profile lazy-imports
  (`-v <product-backend>:/product:ro`), added to `PYTHONPATH`, so the lazy import resolves *at
  runtime*;
- forward the run knobs (e.g. dry-run env) and OAuth (never a billed key).

(The reference `run.sh` for a profile lives beside it in the product's integration
package, not in the engine — it bind-mounts both the integration package and the product
backend exactly this way.)

## Add a new profile — checklist

> Paths below use the in-tree `harness_api/profiles/<name>/` layout for brevity. For a real
> product, put the package **in your own repo** (e.g. `yourco_integration/<name>/`) and point
> `WARDEN_PROFILE` at its dotted module path — the steps are otherwise identical.

A **product** profile lives entirely in your own package (out-of-tree), loaded by a dotted
`WARDEN_PROFILE`. (A *built-in* profile — like the shipped `example` — instead lives in the
engine's `harness_api/profiles/<name>/` and is named bare.) For the out-of-tree case:

1. `<pkg>/<name>/__init__.py`, `profile.py` (`PROFILE = Profile(name, build_factory)`).
2. `<pkg>/<name>/factory.py` — the per-run factory (§3): lazy product import, per-run
   `ToolContext`, `config.custom_tools.tools = <product tools>`.
3. Have the product ship a **seed manifest** (copy allowlist + mkdir list) + a bundle
   builder that validates and packs it (§4). Keep product content in the product repo.
4. `<pkg>/<name>/run.sh` — the bring-up wrapper (§5).
5. `<pkg>/<name>/README.md` — a short profile readme (this one *may* reference the
   product; the general docs here must not).
6. Tests (below).

## Testing a profile

- **Unit (hermetic, no LLM):** the factory injects the expected tools into
  `config.custom_tools.tools`; `job_id == spec.task_id`; the bundle builder validates and
  fails loud on a missing source; the generic `bootstrap` copy/mkdir places exactly the
  declared paths and rejects escaping ones.
- **Integration (dry-run, OAuth):** bring the server up via `run.sh`, `POST /seeds` +
  `/provision`, and drive one real run to a terminal `result`; assert the workspace assembled
  correctly and the tool-call contract holds. The reference profile ships a `driver.py` (in
  its own package) you can mirror.

## Rules (hold these or the seam rots)

1. **Core names no product.** If `grep` for your product noun hits anything outside
   `harness_api/profiles/<name>/`, that's a leak — fix it.
2. **Product imports are lazy and profile-local.** Never import product code at module top
   level; never from the core.
3. **Seed content stays in the product repo.** The profile reads and validates it; it does
   not vendor a copy.
4. **No product wiring in the generic bring-up.** Product docker/env belongs in the
   profile `run.sh`, not `docker/run-harness-api.sh`, and there is no product bed gate.
5. **These docs point down, not up.** A harness doc must not reference an app/product doc;
   the product's docs reference the harness, never the reverse.
