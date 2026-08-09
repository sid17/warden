"""Harness engine config — the **env layer** of the `config/` package.

`HarnessSettings` is built on the harness-local `HarnessBaseSettings` (see
`config/base_settings.py`) — the self-contained, open-source-safe base that carries
only the shared substrate (`environment`, `.env` overlays, behavioral flags). The
harness uses SQLite + local FS + optional S3 — no Postgres/Mongo/Redis — so it
deliberately does NOT inherit any DB/mongo/redis fields or a DB-safety validator.

This is the flat, env-sourced tier: every field carries its existing env alias so
the current env var names keep working verbatim (`.env` overlay loaded once here).
`config/models.py` maps this flat surface into the nested, declarative
``HarnessConfig`` that consumers actually read; `config/build.py` turns that config
into runtime objects. See ``docs/config-plan.md``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field

from warden.config.base_settings import HarnessBaseSettings


class HarnessSettings(HarnessBaseSettings):
    """Harness engine config (HarnessBaseSettings base — no DB/mongo/redis; the harness
    uses SQLite + local FS + optional S3). Additive seam: mirrors the env vars the
    engine reads via os.environ today. Nothing consumes this yet."""

    # -- concurrency / storage roots ---------------------------------------
    harness_concurrency: int = Field(default=8, validation_alias=AliasChoices("WARDEN_CONCURRENCY"))
    harness_base_dir: str = Field(default="data/workspaces", validation_alias=AliasChoices("WARDEN_BASE_DIR"))
    harness_state_root: str = Field(default="data/store", validation_alias=AliasChoices("WARDEN_STATE_ROOT"))
    harness_session_db: str | None = Field(default=None, validation_alias=AliasChoices("WARDEN_SESSION_DB"))
    harness_storage_backend: str = Field(default="local", validation_alias=AliasChoices("WARDEN_STORAGE_BACKEND"))

    # -- S3 / object storage (optional) ------------------------------------
    aws_bucket_name: str | None = Field(default=None, validation_alias=AliasChoices("AWS_BUCKET_NAME"))
    aws_access_key: str | None = Field(default=None, validation_alias=AliasChoices("AWS_ACCESS_KEY", "AWS_ACCESS_KEY_ID"))
    # The standard boto3 access-key env var, kept as its OWN field (not just an
    # alias of aws_access_key) so s3_backend can gate on its presence — boto3's
    # standard credential chain reads AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, so
    # the non-standard AWS_ACCESS_KEY pair is only passed explicitly when the
    # standard id is absent.
    aws_access_key_id: str | None = Field(default=None, validation_alias=AliasChoices("AWS_ACCESS_KEY_ID"))
    aws_secret_access_key: str | None = Field(default=None, validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY"))
    aws_region: str | None = Field(default=None, validation_alias=AliasChoices("AWS_REGION", "AWS_DEFAULT_REGION"))
    aws_bucket_location: str | None = Field(default=None, validation_alias=AliasChoices("AWS_BUCKET_LOCATION"))
    aws_s3_endpoint: str | None = Field(default=None, validation_alias=AliasChoices("AWS_S3_ENDPOINT"))

    # -- observability (Langfuse / OTEL) -----------------------------------
    langfuse_public_key: str | None = Field(default=None, validation_alias=AliasChoices("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str | None = Field(default=None, validation_alias=AliasChoices("LANGFUSE_SECRET_KEY"))
    langfuse_host: str = Field(default="http://localhost:3456", validation_alias=AliasChoices("LANGFUSE_HOST"))
    langfuse_tool_output_limit: int = Field(default=500, validation_alias=AliasChoices("LANGFUSE_TOOL_OUTPUT_LIMIT"))
    otel_collector_endpoint: str = Field(default="http://localhost:4317", validation_alias=AliasChoices("OTEL_COLLECTOR_ENDPOINT"))
    otel_service_name: str | None = Field(default=None, validation_alias=AliasChoices("OTEL_SERVICE_NAME"))
    enable_telemetry: bool = Field(default=False, validation_alias=AliasChoices("CLAUDE_CODE_ENABLE_TELEMETRY"))

    # -- audit -------------------------------------------------------------
    audit_enabled: bool = Field(default=False, validation_alias=AliasChoices("AUDIT_ENABLED"))
    audit_log_dir: str | None = Field(default=None, validation_alias=AliasChoices("AUDIT_LOG_DIR"))
    audit_run_id: str = Field(default="run-default", validation_alias=AliasChoices("AUDIT_RUN_ID"))

    # -- openharness (local LLM provider) ----------------------------------
    # Defaults read from providers/openharness/session.py:
    #   _DEFAULT_MODEL = "qwen3:1.7b"; _DEFAULT_BASE_URL = "http://localhost:11434"
    openharness_model: str = Field(default="qwen3:1.7b", validation_alias=AliasChoices("OPENHARNESS_MODEL"))
    openharness_base_url: str = Field(default="http://localhost:11434", validation_alias=AliasChoices("OPENHARNESS_BASE_URL"))
    openharness_api_key: str = Field(default="ollama", validation_alias=AliasChoices("OPENHARNESS_API_KEY"))
    codex_model: str = Field(default="gpt-5.4", validation_alias=AliasChoices("CODEX_MODEL"))
    # Opt-in: deliver custom tools to codex via an in-proc MCP server. These tools
    # are UNGATED (codex MCP calls ride the elicitation path; can_use_tool is never
    # consulted for them). Default False → passing custom_tools to codex raises
    # (fail-closed). exec/patch gating is unaffected.
    codex_allow_ungated_custom_tools: bool = Field(default=False, validation_alias=AliasChoices("CODEX_ALLOW_UNGATED_CUSTOM_TOOLS"))

    # -- provider credentials ----------------------------------------------
    anthropic_api_key: str | None = Field(default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    claude_code_oauth_token: str | None = Field(default=None, validation_alias=AliasChoices("CLAUDE_CODE_OAUTH_TOKEN"))
    openai_api_key: str | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))
    # Axis-2 (managed keys / pricing) deliberately does NOT live here — the engine
    # is account-agnostic (§8). MANAGED_KEYS_JSON/FILE + PRICING_JSON belong to the
    # account/billing layer: warden/harness_api/config.py HarnessApiConfig.

    # -- safety classifier knobs (promoted from hardcoded defaults) ---------
    safety_llm_judge_model_anthropic: str = Field(default="claude-haiku-4-5-20251001", validation_alias=AliasChoices("SAFETY_LLM_JUDGE_MODEL_ANTHROPIC"))
    safety_llm_judge_model_openai: str = Field(default="gpt-4o-mini", validation_alias=AliasChoices("SAFETY_LLM_JUDGE_MODEL_OPENAI"))
    safety_ollama_model: str = Field(default="gemma3:4b", validation_alias=AliasChoices("SAFETY_OLLAMA_MODEL"))
    safety_ollama_base_url: str = Field(default="http://localhost:11434", validation_alias=AliasChoices("SAFETY_OLLAMA_BASE_URL"))
    safety_deberta_model_id: str = Field(default="protectai/deberta-v3-base-prompt-injection-v2", validation_alias=AliasChoices("SAFETY_DEBERTA_MODEL_ID"))
    safety_fuzzy_threshold: float = Field(default=0.6, validation_alias=AliasChoices("SAFETY_FUZZY_THRESHOLD"))
    safety_streaming_buffer_size: int = Field(default=200, validation_alias=AliasChoices("SAFETY_STREAMING_BUFFER_SIZE"))


@lru_cache
def get_harness_settings() -> "HarnessSettings":
    return HarnessSettings()
