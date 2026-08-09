"""The builder — declarative ``HarnessConfig`` → runtime seam objects.

``config/models.py`` holds declarative *values*; this module is the one place
that turns them into the runtime objects the drive path wires into the
orchestrator: the permission handler, the tool scope, the middleware pipeline,
the persistence backend, and the effective provider model. Keeping the
name→class knowledge here lets the config surface stay import-light (only
``pydantic-settings`` + pydantic) — heavy seam modules are imported lazily inside
each builder, so ``import config.build`` is cheap and the builders are
unit-testable in isolation.

This is the "declarative + builder" half of the decision in
``docs/config-plan.md``: the config layer describes *what*; ``build.py`` decides
*how* to construct it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from warden.config.models import (
    CustomToolsConfig,
    MiddlewareConfig,
    PermissionsConfig,
    PersistenceConfig,
    ProviderConfig,
    SafetyConfig,
    WorkspaceConfig,
)

if TYPE_CHECKING:
    from warden.persistence.backend import StorageBackend
    from warden.persistence.config import (
        PersistenceConfig as RuntimePersistenceConfig,
    )
    from warden.schemas.tool_scope import ToolScope
    from warden.seams.middleware import Middleware
    from warden.seams.permissions import PermissionHandler
    from warden.workspace.workflow.middleware import WorkflowMiddleware


# --- permissions --------------------------------------------------------------


def build_permission_handler(cfg: PermissionsConfig) -> "PermissionHandler":
    """Construct the permission handler the transport uses.

    ``handler_instance`` is the escape hatch: an app that already has a bespoke
    handler passes it directly and it wins over ``handler``. Otherwise the
    declarative ``handler`` kind selects a built-in. ``durable_http`` (M6) is the
    durable HITL transport: it needs per-run context (run id, event egress, the
    durable store), so it can't be built from config alone — the Runner builds the
    wired handler and injects it via ``ChatAPI.set_permission_handler`` before
    ``init()``. Until then this returns a **fail-closed placeholder** that DENIES
    every consult (never auto-allow): if a ``durable_http`` run is ever driven
    without the Runner wiring it, tools are refused loudly rather than silently
    permitted.
    """
    if cfg.handler_instance is not None:
        return cfg.handler_instance

    from warden.seams.permissions import (
        AutoAllowHandler,
        CLIPermissionHandler,
    )

    if cfg.handler == "auto_allow":
        return AutoAllowHandler()
    if cfg.handler == "cli":
        return CLIPermissionHandler()
    if cfg.handler == "durable_http":
        from warden.seams.defer import UnwiredDurableHandler

        return UnwiredDurableHandler()
    raise ValueError(f"unknown permission handler kind: {cfg.handler!r}")


# --- tool scope ---------------------------------------------------------------


def build_tool_scope(cfg: PermissionsConfig) -> "ToolScope | None":
    """Build a :class:`ToolScope` from the allow/deny lists, or ``None`` when
    neither is set (all tools permitted — the orchestrator treats ``None`` as
    "no restriction")."""
    if cfg.allowed_tools is None and cfg.denied_tools is None:
        return None

    from warden.schemas.tool_scope import ToolScope

    return ToolScope(allowed=cfg.allowed_tools, denied=cfg.denied_tools)


# --- middleware ---------------------------------------------------------------


def _classifier_battery(name: str, safety: SafetyConfig):
    """Resolve a battery NAME → ``(Classifier instance, allow_authority)``.

    The cascade's ORDER (``safety.cascade.members``) names entries here; each
    yields the classifier plus whether its confident "safe" may STOP the cascade
    (``allow_authority``). Heavy modules are imported LAZILY inside each branch so
    naming ``regex-input``/``llm-judge`` (the torch-free default) never loads
    torch — only ``deberta`` does, and only when named. Unknown name → fail-fast.
    """
    if name == "regex-input":
        # Cheap heuristic: "safe" only means "no known bad pattern" — NOT an
        # allow-authority, so its safe verdict escalates rather than accepting.
        from warden.safety.middleware.classifiers.regex_input import (
            RegexInputClassifier,
        )

        return RegexInputClassifier(), False
    if name == "llm-judge":
        from warden.safety.middleware.classifiers.llm_judge import (
            LLMJudgeInputClassifier,
        )

        return (
            LLMJudgeInputClassifier(
                provider="anthropic",
                model=safety.classifiers.llm_judge_model_anthropic,
            ),
            True,
        )
    if name == "ollama-guard":
        from warden.safety.middleware.classifiers.ollama_guard import (
            OllamaGuardInputClassifier,
        )

        return (
            OllamaGuardInputClassifier(
                model=safety.classifiers.ollama_model,
                base_url=safety.classifiers.ollama_base_url,
            ),
            True,
        )
    if name == "deberta":
        # Opt-in: the ctor LOADS torch (requires the `reranker` extra). Only
        # instantiated when explicitly named in the cascade members.
        from warden.safety.middleware.classifiers.deberta_onnx import (
            DeBertaONNXClassifier,
        )

        return DeBertaONNXClassifier(), True
    raise ValueError(
        f"unknown cascade classifier battery: {name!r} "
        "(known: 'regex-input', 'llm-judge', 'ollama-guard', 'deberta')"
    )


def _build_cascade(safety: SafetyConfig):
    """Build the production ``CascadeMiddleware`` from ``safety.cascade``.

    Each member name resolves through ``_classifier_battery`` into an ordered
    ``CascadeStage`` (cheapest→heaviest); the short-circuit thresholds and
    default come straight from the declared config.
    """
    from warden.safety.middleware.input.cascade import (
        CascadeMiddleware,
        CascadeStage,
    )

    cascade = safety.cascade
    stages = [
        CascadeStage(classifier=clf, allow_authority=authority)
        for clf, authority in (
            _classifier_battery(name, safety) for name in cascade.members
        )
    ]
    return CascadeMiddleware(
        stages,
        block_threshold=cascade.block_threshold,
        allow_threshold=cascade.allow_threshold,
        default_allow=cascade.default_allow,
    )


def _input_middleware_registry(safety: SafetyConfig):
    """Map declarative input-middleware names to constructed instances.

    Built lazily so importing this module never pulls in the safety package.
    Classifier-backed middleware read their knobs from ``safety.classifiers`` —
    this is where the two config sections (``middleware`` = composition,
    ``safety`` = classifier backends) merge into one pipeline. ``fuzzy-intent``
    threads the promoted ``fuzzy_threshold``; ``cascade`` resolves the
    config-declared production cascade (``safety.cascade``); the pattern/regex
    middleware are settings-free today.
    """
    from warden.safety.middleware.input.intent import (
        FuzzyIntentClassifier,
        IntentClassifierMiddleware,
    )
    from warden.safety.middleware.input.sanitize import (
        E3ExpandedMiddleware,
        SanitizeMiddleware,
    )

    return {
        "sanitize": lambda: SanitizeMiddleware(),
        "e3-expanded": lambda: E3ExpandedMiddleware(),
        "intent": lambda: IntentClassifierMiddleware(),
        "fuzzy-intent": lambda: FuzzyIntentClassifier(
            threshold=safety.classifiers.fuzzy_threshold
        ),
        "cascade": lambda: _build_cascade(safety),
    }


def _output_middleware_registry(safety: SafetyConfig):
    """Map declarative output-middleware names → constructed instances.

    Symmetric to ``_input_middleware_registry``: the built config CARRIES output
    names, resolved here to seam-conforming ``PassThroughMiddleware`` instances.
    ``leak-filter`` threads the promoted ``streaming_buffer_size`` from
    ``safety.classifiers`` (the same merge point as the input side); ``redact`` is
    settings-free. Imported lazily so ``import config.build`` never pulls in the
    safety package. Unknown name → fail-fast (see ``build_middleware``).
    """
    from warden.safety.middleware.output.middleware import (
        RedactOutputMiddleware,
        StreamingLeakFilterMiddleware,
    )

    return {
        "leak-filter": lambda: StreamingLeakFilterMiddleware(
            buffer_size=safety.classifiers.streaming_buffer_size
        ),
        "redact": lambda: RedactOutputMiddleware(),
    }


def build_middleware(
    mw: MiddlewareConfig, safety: SafetyConfig
) -> tuple[list["Middleware"], list["Middleware"]]:
    """Build the (input, output) middleware pipelines.

    Both directions are SYMMETRIC (SAFE-5): declarative ``input``/``output`` names
    resolve through their registry (which merges the ``safety`` classifier knobs),
    then the bespoke ``*_instances`` are appended verbatim. An unknown name in
    either direction is a hard error (defensive fallback for the raw-``str`` bypass
    of the §4b Literal allow-list) rather than a silent no-op. Each master switch
    (§4a) OFF ⇒ that direction is a hard no-op empty pipeline regardless of any
    configured names/instances.
    """
    # §4a master switch: OFF ⇒ hard no-op INPUT pipeline regardless of any
    # configured input names/instances (mirrors the output gate below).
    if mw.enable_input_middleware:
        registry = _input_middleware_registry(safety)
        input_pipeline: list[Middleware] = []
        for name in mw.input:
            factory = registry.get(name)
            if factory is None:
                raise ValueError(
                    f"unknown input middleware name: {name!r} "
                    f"(known: {sorted(registry)})"
                )
            input_pipeline.append(factory())
        input_pipeline.extend(mw.input_instances)
    else:
        input_pipeline = []

    # §4a master switch: OFF ⇒ hard no-op output pipeline regardless of any
    # configured output names/instances (the orchestrator sees an empty list and
    # the drain loop is a transparent pass-through).
    if not mw.enable_output_middleware:
        return input_pipeline, []

    out_registry = _output_middleware_registry(safety)
    output_pipeline: list[Middleware] = []
    for name in mw.output:
        factory = out_registry.get(name)
        if factory is None:
            raise ValueError(
                f"unknown output middleware name: {name!r} "
                f"(known: {sorted(out_registry)})"
            )
        output_pipeline.append(factory())
    output_pipeline.extend(mw.output_instances)

    return input_pipeline, output_pipeline


def apply_workflow_middleware(
    base: MiddlewareConfig, wf_mw: "WorkflowMiddleware | None"
) -> MiddlewareConfig:
    """Merge a workflow's declared middleware into a NEW effective config (SAFE-5).

    The workflow's safety policy travels with its manifest: declared ``input``/
    ``output`` NAMES EXTEND the base lists (appended, de-nothing), and
    ``enable_input``/``enable_output`` OVERRIDE the base master switches when set
    (non-None). Bespoke ``*_instances`` are carried through untouched (a YAML can't
    declare them). No-op passthrough when ``wf_mw is None`` — returns ``base``
    unchanged (same object). Never mutates ``base`` in place.
    """
    if wf_mw is None:
        return base

    return base.model_copy(
        update={
            "input": [*base.input, *wf_mw.input],
            "output": [*base.output, *wf_mw.output],
            "enable_input_middleware": (
                wf_mw.enable_input
                if wf_mw.enable_input is not None
                else base.enable_input_middleware
            ),
            "enable_output_middleware": (
                wf_mw.enable_output
                if wf_mw.enable_output is not None
                else base.enable_output_middleware
            ),
        }
    )


def apply_workflow_event_map(
    base: CustomToolsConfig, wf_event_map: dict[str, str] | None
) -> CustomToolsConfig:
    """Merge a workflow's ``event_tool_map`` into a NEW effective CustomToolsConfig
    (EXT-P1/A2, mirrors :func:`apply_workflow_middleware`).

    The workflow's re-tag policy travels with its manifest: the declared
    ``{tool_name → event_type}`` entries EXTEND the base map (the manifest wins on a
    key clash). No-op passthrough when ``wf_event_map`` is falsy — returns ``base``
    unchanged. Never mutates ``base`` in place. Values are already load-validated
    against the known event-type set (``loader._validate_event_tool_map``)."""
    if not wf_event_map:
        return base
    return base.model_copy(
        update={"event_tool_map": {**base.event_tool_map, **wf_event_map}}
    )


# --- persistence --------------------------------------------------------------


def build_persistence(
    persistence: PersistenceConfig, workspace: WorkspaceConfig
) -> tuple["RuntimePersistenceConfig", "StorageBackend"]:
    """Bridge the config-layer persistence section to the low-level runtime
    struct + storage backend.

    Returns the low-level ``persistence.config.PersistenceConfig`` (imported
    under an alias to keep it distinct from the config-layer ``PersistenceConfig``
    passed in) and the selected ``StorageBackend``. The S3 branch requires a
    bucket; the local branch writes tarballs under ``state_root``. Restore is
    the caller's job (``workspace.ensure_restored``) — this only constructs.
    """
    from warden.persistence.config import (
        PersistenceConfig as RuntimePersistenceConfig,
    )
    from warden.persistence.factory import get_backend

    runtime_cfg = RuntimePersistenceConfig(
        base_dir=Path(workspace.base_dir),
        state_root=Path(persistence.state_root),
        prefix=persistence.prefix,
        exclude_patterns=persistence.exclude_patterns,
    )

    if persistence.backend == "s3":
        s3 = persistence.s3
        bucket = s3.bucket
        if not bucket:
            raise ValueError(
                "persistence.backend='s3' requires persistence.s3.bucket "
                "(AWS_BUCKET_NAME)"
            )
        # C7 (M8): thread the whole S3Config slice to the backend leaf so it
        # never reads get_harness_settings() itself. bucket_location wins over
        # region (mirrors the prior in-backend precedence).
        backend = get_backend(
            "s3",
            bucket=bucket,
            prefix=s3.prefix,
            exclude_patterns=persistence.exclude_patterns,
            endpoint_url=s3.endpoint,
            region=s3.bucket_location or s3.region,
            access_key_id=s3.access_key_id,
            access_key=s3.access_key,
            secret_access_key=s3.secret_access_key,
        )
    else:
        backend = get_backend(
            "local",
            state_root=runtime_cfg.state_root,
            exclude_patterns=persistence.exclude_patterns,
        )

    return runtime_cfg, backend


# --- provider -----------------------------------------------------------------


def resolve_model(cfg: ProviderConfig) -> str | None:
    """Resolve the effective model id for the selected provider.

    An explicit ``model`` always wins. Otherwise the promoted per-provider
    default applies (``codex_model`` / ``openharness_model``); ``claude`` returns
    ``None`` so the SDK's own default is used. Centralizes the model default that
    each provider session currently hardcodes.
    """
    if cfg.model is not None:
        return cfg.model
    if cfg.provider == "codex":
        return cfg.codex_model
    if cfg.provider == "openharness":
        return cfg.openharness_model
    return None
