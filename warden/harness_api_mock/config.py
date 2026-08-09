"""``MockConfig`` — the mock harness service's typed settings (§10).

Reuses the harness-local ``HarnessBaseSettings`` base (``.env`` overlays) so env
loading matches the rest of the engine without a monorepo dependency. Every knob is
env-overridable; tests construct ``MockConfig(...)`` by name (``populate_by_name``
on the base).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field

from warden.config.base_settings import HarnessBaseSettings

# This file is warden/harness_api_mock/config.py → parents[3] is repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class MockConfig(HarnessBaseSettings):
    """The mock harness service config (§10)."""

    port: int = Field(
        default=9000, validation_alias=AliasChoices("MOCK_WARDEN_PORT")
    )
    service_token: str = Field(
        default="mock-secret-token",
        validation_alias=AliasChoices("MOCK_WARDEN_SERVICE_TOKEN"),
    )
    # The active profile. Selects ``profiles/<profile>/`` (bare name → built-in
    # profiles pkg) or an out-of-tree package (dotted, fully-qualified name) — its
    # scripts, fixtures, and writeback invoker. One mock process serves one profile.
    # Defaults to the product-agnostic ``example`` so the engine names no product; a
    # product overrides via ``MOCK_WARDEN_PROFILE`` (e.g. a dotted integration path).
    profile: str = Field(
        default="example",
        validation_alias=AliasChoices("MOCK_WARDEN_PROFILE"),
    )
    # Optional override for where canned fixtures live (one subdir per workflow),
    # copied into a run's workspace at submit time so ``GET /file`` serves real bytes.
    # Empty (default) → the active profile's own ``fixtures/`` dir is used.
    fixture_dir: str = Field(
        default="",
        validation_alias=AliasChoices("MOCK_WARDEN_FIXTURE_DIR"),
    )
    # Per-run file roots live under here as ``<workspace_root>/<run_id>/``.
    workspace_root: str = Field(
        default=str(_REPO_ROOT / "state" / "mock_harness" / "workspaces"),
        validation_alias=AliasChoices("MOCK_WARDEN_WORKSPACE_ROOT"),
    )
    # Where the durable run-events log lives (tests point this at a temp dir).
    event_log_dir: str = Field(
        default=str(_REPO_ROOT / "state" / "mock_harness"),
        validation_alias=AliasChoices("MOCK_WARDEN_EVENT_LOG_DIR"),
    )
    # Streaming realism: each SleepStep sleeps ``seconds * step_delay_s``. 0.0 in
    # tests (instant), prod-like via env.
    step_delay_s: float = Field(
        default=0.0, validation_alias=AliasChoices("MOCK_WARDEN_STEP_DELAY_S")
    )
    # Gate auto-deny SLA (seconds). 300 by default; tests set ~0.05.
    sla_seconds: float = Field(
        default=300.0, validation_alias=AliasChoices("MOCK_WARDEN_SLA_SECONDS")
    )
    # Which N1 tool seam to use: ``noop`` (canned, product-free) or ``profile`` (the
    # active profile's real writeback invoker, e.g. your product's tool invoker).
    tool_invoker_mode: str = Field(
        default="noop", validation_alias=AliasChoices("MOCK_WARDEN_TOOL_INVOKER_MODE")
    )
    # --- profile invoker wiring (only read when tool_invoker_mode=="profile") ----
    # The product API base URL the profile's real tools POST writeback to. The active
    # profile decides the exact path shape (e.g. a profile might POST ``{base}/jobs/{id}...``
    # under an app-specific prefix). Empty (default) → set by the product's stack when the
    # real loop runs; unused in ``noop`` mode.
    product_api_url: str = Field(
        default="",
        validation_alias=AliasChoices("MOCK_WARDEN_PRODUCT_API_URL"),
    )
    # The bearer token the tools attach on writeback (a static minted user JWT for the
    # e2e; per-run minting is task-8). Empty => no Authorization header.
    product_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("MOCK_WARDEN_PRODUCT_API_TOKEN"),
    )
    # Whether the profile invoker's tools fire the real POST (apply) vs return a
    # dry-run payload (ToolContext.should_apply). True for the real e2e loop.
    tools_apply: bool = Field(
        default=True, validation_alias=AliasChoices("MOCK_WARDEN_TOOLS_APPLY")
    )
    # Optional invocation audit log path forwarded into the tools' ToolContext so the
    # mock's real-tool calls land in the same JSONL.
    product_tools_log: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MOCK_WARDEN_PRODUCT_TOOLS_LOG"),
    )
    # Fault-injection knobs (§7). ``inject_error_at`` emits an error at step N (1-based
    # step index); ``budget_stop`` emits a ``stopped{budget}`` terminal.
    inject_error_at: int | None = Field(
        default=None, validation_alias=AliasChoices("MOCK_WARDEN_INJECT_ERROR_AT")
    )
    budget_stop: bool = Field(
        default=False, validation_alias=AliasChoices("MOCK_WARDEN_BUDGET_STOP")
    )
