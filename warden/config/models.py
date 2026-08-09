"""The nested, declarative harness config — the surface consumers read/set.

``HarnessConfig`` is a plain ``BaseModel`` composed of small per-concern sub-configs
(the "mega config with small classes"). Env-sourced fields are populated from the
flat ``HarnessSettings`` env layer via :meth:`HarnessConfig.from_settings`;
programmatic fields (middleware pipeline, permission handler kind, custom tools)
default here and are set by the app. ``config/build.py`` turns this declarative
config into runtime seam objects. See ``docs/config-plan.md``.

Kinds of field:  [env] flat env-sourced · [decl] declarative value → builder ·
[obj] escape-hatch instance the app passes directly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from warden.config.settings import HarnessSettings

# Mirror persistence.config.DEFAULT_EXCLUDE_PATTERNS without importing the heavy
# module here (config stays dependency-light); the builder reconciles them.
_DEFAULT_EXCLUDE = ("node_modules/", ".venv/", "__pycache__/", "*.pyc")

# §4b: the declared, discoverable allow-list of middleware NAMES. Typing the
# ``MiddlewareConfig.input``/``output`` fields with these Literals moves an
# unknown name to a pydantic ValidationError at config CONSTRUCTION (declared and
# discoverable), symmetric input↔output. ``config/build.py`` keeps a defensive
# runtime ValueError for the raw-``str`` bypass path. Keep these in lockstep with
# the registries in ``config/build.py``.
InputMiddlewareName = Literal[
    "sanitize", "e3-expanded", "intent", "fuzzy-intent", "cascade"
]
OutputMiddlewareName = Literal["leak-filter", "redact"]


class ProviderConfig(BaseModel):
    provider: str = "claude"                                    # decl
    model: str | None = None                                    # decl
    openharness_model: str = "qwen3:1.7b"                       # env
    openharness_base_url: str = "http://localhost:11434"        # env
    openharness_api_key: str = "ollama"                         # env
    codex_model: str = "gpt-5.4"                                # env
    # Opt-in for UNGATED codex custom-tool delivery via in-proc MCP (default off →
    # passing custom_tools to codex raises; exec/patch gating unaffected).
    codex_allow_ungated_custom_tools: bool = False              # env


class AuthConfig(BaseModel):
    anthropic_api_key: str | None = None                        # env
    claude_code_oauth_token: str | None = None                  # env
    openai_api_key: str | None = None                           # env
    auth_env: dict[str, str] | None = None                      # obj (per-run injection)


class WorkspaceConfig(BaseModel):
    base_dir: str = "data/workspaces"                           # env
    user_id: str = "default"                                    # decl
    task_id: str | None = None                                  # decl
    skills_allowlist: list[str] = Field(default_factory=list)   # decl (bootstrap)
    agents_allowlist: list[str] = Field(default_factory=list)   # decl (bootstrap)


class PermissionsConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    handler: Literal["auto_allow", "cli", "durable_http"] = "auto_allow"  # decl → builder
    handler_instance: Any | None = None                         # obj (escape hatch)
    allowed_tools: list[str] | None = None                      # decl → ToolScope
    denied_tools: list[str] | None = None                       # decl → ToolScope
    workflow: str | None = None                                 # decl


class MiddlewareConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    input: list[InputMiddlewareName] = Field(default_factory=list)   # decl (names → pipeline, §4b allow-list)
    output: list[OutputMiddlewareName] = Field(default_factory=list)  # decl (§4b allow-list)
    input_instances: list[Any] = Field(default_factory=list)    # obj (bespoke Middleware)
    output_instances: list[Any] = Field(default_factory=list)   # obj
    enable_input_middleware: bool = False                       # decl (master switch, §4a)
    enable_output_middleware: bool = False                      # decl (master switch, §4a)


class CustomToolsConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    tools: list[Any] = Field(default_factory=list)              # obj (list[CustomTool])
    mcp_server: str | None = None                               # decl (universal path)
    # EXT-P1/A2 (E4): {tool_name → event_type} — the runner re-tags a named
    # custom-tool call into a typed egress event (data opaque). Travels with the
    # workflow manifest (apply_workflow_event_map); values validated at load.
    event_tool_map: dict[str, str] = Field(default_factory=dict)  # decl


class ClassifierSettings(BaseModel):
    llm_judge_model_anthropic: str = "claude-haiku-4-5-20251001"  # env
    llm_judge_model_openai: str = "gpt-4o-mini"                    # env
    ollama_model: str = "gemma3:4b"                                # env
    ollama_base_url: str = "http://localhost:11434"               # env
    deberta_model_id: str = "protectai/deberta-v3-base-prompt-injection-v2"  # env
    fuzzy_threshold: float = 0.6                                  # env
    streaming_buffer_size: int = 200                             # env


class CascadeConfig(BaseModel):
    """Config-declared production cascade (§4c): the ORDERED battery + policy.

    ``members`` is the cascade ORDER — battery names cheapest→heaviest (the
    torch-free default: cheap regex heuristic then the LLM judge). This is a
    separate concern from ``ClassifierSettings`` (per-classifier model knobs):
    this holds the composition + short-circuit thresholds, that holds the
    backend knobs. The builder resolves each name via ``_classifier_battery``.
    """
    members: list[str] = Field(
        default_factory=lambda: ["regex-input", "llm-judge"]
    )                                                           # decl (ordered)
    block_threshold: float = 0.5                                # decl
    allow_threshold: float = 0.8                                # decl
    default_allow: bool = True                                  # decl


class PathRule(BaseModel):
    """One PreToolUse path-restriction rule (SAFE-6). Consumes the same shape the
    M5 ``derive_manifest`` emits, so operator-copied output enforces directly."""
    match_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Grep", "Glob", "Write", "Edit", "MultiEdit"]
    )                                                           # decl
    allow_path_globs: list[str] = []                            # decl (empty => no glob restriction)
    on_violation: Literal["deny"] = "deny"                      # decl


class PathHookConfig(BaseModel):
    """Master switch + rules for the SAFE-6 PreToolUse path-enforcement hook.

    Fires for ALL tools — including the auto-allowed reads (Read/Grep/Glob) that
    ``can_use_tool`` never sees — giving per-path restriction a real enforcement
    point. Default OFF (opt-in)."""
    enabled: bool = False                                       # decl (master switch)
    rules: list[PathRule] = Field(default_factory=list)         # decl
    deny_sensitive: bool = True                                 # decl (also deny check_sensitive matches)


class DurableDeferConfig(BaseModel):
    """Master switch + store root for the durable HITL defer path (pre-07b).

    When enabled on Claude, a PreToolUse hook consults a file-backed
    ``FileDeferStore`` at ``store_root``: an unresolved call returns
    ``permissionDecision:"defer"`` (the run ends with ``deferred_tool_use`` — the
    pending call is ejected to disk, no in-memory future); a later resume in a
    fresh process re-fires the hook for the SAME ``tool_use_id`` and injects the
    recorded allow/deny (exact-id). ``store_root`` is a directory path so two
    processes over the same root share the durable records. Default OFF."""
    enabled: bool = False                                       # decl (master switch)
    store_root: str | None = None                              # decl (dir shared across processes)


class SafetyConfig(BaseModel):
    system_prompt: str | None = None                            # decl
    classifiers: ClassifierSettings = Field(default_factory=ClassifierSettings)
    cascade: CascadeConfig = Field(default_factory=CascadeConfig)
    path_hook: PathHookConfig = Field(default_factory=PathHookConfig)
    durable_defer: DurableDeferConfig = Field(default_factory=DurableDeferConfig)
    # SAFE-4: opt-in canary backstop — plant a token in the system prompt + check
    # it per output chunk. INDEPENDENT of enable_output_middleware (its own gate).
    # ``canary_token=None`` => the default DEFAULT_CANARY constant (tests pin one).
    enable_canary: bool = False                                 # decl (master switch)
    canary_token: str | None = None                            # decl (None => default token)


class ContinuationConfig(BaseModel):
    """B1 — master switch for the Claude top-level ``Stop`` continuation hook.

    When ``enabled``, the Claude session installs a ``Stop`` hook that blocks an
    early ``stop_reason=end_turn`` and re-prompts IN-STREAM (same session) until a
    named completion tool (``until_tool``) fires — so a multi-agent orchestrator
    that dispatches sub-agents then ends its turn before the pipeline's final
    custom tool no longer terminates the run prematurely. ``max_turns`` is an
    OUTER safety cap the session sets on ``ClaudeAgentOptions`` when continuation
    is on (so a mis-behaving loop is still bounded). Default OFF — only a profile
    (a profile) enables it, so no other flow changes. Claude-only."""
    enabled: bool = False                                       # decl (master switch)
    until_tool: str = ""                                        # decl (bare completion-tool name)
    directive: str = ""                                         # decl ("" => hook's DEFAULT_DIRECTIVE)
    max_turns: int = 40                                         # decl (outer cap when enabled)


class TelemetryConfig(BaseModel):
    otel_collector_endpoint: str = "http://localhost:4317"      # env
    otel_service_name: str | None = None                        # env
    enable_telemetry: bool = False                              # env
    langfuse_public_key: str | None = None                      # env
    langfuse_secret_key: str | None = None                      # env
    langfuse_host: str = "http://localhost:3456"                # env
    langfuse_tool_output_limit: int = 500                       # env


class AuditConfig(BaseModel):
    enabled: bool = False                                       # env
    run_id: str = "run-default"                                 # env
    log_dir: str | None = None                                  # env


class ObservabilityConfig(BaseModel):
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)


class S3Config(BaseModel):
    bucket: str | None = None                                   # env
    prefix: str = ""                                            # decl
    region: str | None = None                                  # env
    endpoint: str | None = None                                # env
    bucket_location: str | None = None                         # env
    access_key_id: str | None = None                           # env
    access_key: str | None = None                              # env
    secret_access_key: str | None = None                       # env


class PersistenceConfig(BaseModel):
    """Config-layer persistence section (distinct from the low-level
    ``persistence.config.PersistenceConfig`` struct the builder constructs)."""
    backend: Literal["local", "s3"] = "local"                  # env
    state_root: str = "data/store"                             # env
    prefix: str = "v1"                                         # decl
    exclude_patterns: tuple[str, ...] = _DEFAULT_EXCLUDE       # decl
    session_db_path: str | None = None                        # env
    s3: S3Config = Field(default_factory=S3Config)


class ConcurrencyConfig(BaseModel):
    max_concurrent: int = 8                                    # env (Runner Semaphore)


class HarnessConfig(BaseModel):
    """The engine config — one object, per-concern sub-configs. Account/billing
    (Axis-2) lives separately in ``harness_api``'s ``HarnessApiConfig`` (§8)."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    middleware: MiddlewareConfig = Field(default_factory=MiddlewareConfig)
    custom_tools: CustomToolsConfig = Field(default_factory=CustomToolsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    continuation: ContinuationConfig = Field(default_factory=ContinuationConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)

    @classmethod
    def from_settings(cls, s: HarnessSettings) -> "HarnessConfig":
        """Map the flat env layer into the nested config. Programmatic sections
        (permissions/middleware/custom_tools) keep their defaults for the app to set."""
        return cls(
            provider=ProviderConfig(
                openharness_model=s.openharness_model,
                openharness_base_url=s.openharness_base_url,
                openharness_api_key=s.openharness_api_key,
                codex_model=s.codex_model,
                codex_allow_ungated_custom_tools=s.codex_allow_ungated_custom_tools,
            ),
            auth=AuthConfig(
                anthropic_api_key=s.anthropic_api_key,
                claude_code_oauth_token=s.claude_code_oauth_token,
                openai_api_key=s.openai_api_key,
            ),
            workspace=WorkspaceConfig(base_dir=s.harness_base_dir),
            safety=SafetyConfig(
                classifiers=ClassifierSettings(
                    llm_judge_model_anthropic=s.safety_llm_judge_model_anthropic,
                    llm_judge_model_openai=s.safety_llm_judge_model_openai,
                    ollama_model=s.safety_ollama_model,
                    ollama_base_url=s.safety_ollama_base_url,
                    deberta_model_id=s.safety_deberta_model_id,
                    fuzzy_threshold=s.safety_fuzzy_threshold,
                    streaming_buffer_size=s.safety_streaming_buffer_size,
                ),
            ),
            observability=ObservabilityConfig(
                telemetry=TelemetryConfig(
                    otel_collector_endpoint=s.otel_collector_endpoint,
                    otel_service_name=s.otel_service_name,
                    enable_telemetry=s.enable_telemetry,
                    langfuse_public_key=s.langfuse_public_key,
                    langfuse_secret_key=s.langfuse_secret_key,
                    langfuse_host=s.langfuse_host,
                    langfuse_tool_output_limit=s.langfuse_tool_output_limit,
                ),
                audit=AuditConfig(
                    enabled=s.audit_enabled,
                    run_id=s.audit_run_id,
                    log_dir=s.audit_log_dir,
                ),
            ),
            persistence=PersistenceConfig(
                backend=s.harness_storage_backend,  # type: ignore[arg-type]
                state_root=s.harness_state_root,
                session_db_path=s.harness_session_db,
                s3=S3Config(
                    bucket=s.aws_bucket_name,
                    region=s.aws_region,
                    endpoint=s.aws_s3_endpoint,
                    bucket_location=s.aws_bucket_location,
                    access_key_id=s.aws_access_key_id,
                    access_key=s.aws_access_key,
                    secret_access_key=s.aws_secret_access_key,
                ),
            ),
            concurrency=ConcurrencyConfig(max_concurrent=s.harness_concurrency),
        )
