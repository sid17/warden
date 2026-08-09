"""P2 tests for the config builder — declarative HarnessConfig sub-configs turn
into runtime seam objects (permission handler, tool scope, middleware pipeline,
persistence backend, effective model).

Each builder is exercised in isolation; no orchestrator/provider deps are pulled
in beyond the seam objects the builder itself constructs.
"""

import pytest
from pydantic import ValidationError

from warden.config.build import (
    apply_workflow_middleware,
    build_middleware,
    build_permission_handler,
    build_persistence,
    build_tool_scope,
    resolve_model,
)
from warden.config.models import (
    MiddlewareConfig,
    PermissionsConfig,
    PersistenceConfig,
    ProviderConfig,
    S3Config,
    SafetyConfig,
    WorkspaceConfig,
)


# --- permissions --------------------------------------------------------------


def test_permission_handler_auto_allow():
    from warden.seams.permissions import AutoAllowHandler

    h = build_permission_handler(PermissionsConfig(handler="auto_allow"))
    assert isinstance(h, AutoAllowHandler)


def test_permission_handler_cli():
    from warden.seams.permissions import CLIPermissionHandler

    h = build_permission_handler(PermissionsConfig(handler="cli"))
    assert isinstance(h, CLIPermissionHandler)


def test_permission_handler_instance_escape_hatch_wins():
    sentinel = object()
    h = build_permission_handler(
        PermissionsConfig(handler="cli", handler_instance=sentinel)
    )
    assert h is sentinel


def test_permission_handler_durable_http_returns_fail_closed_placeholder():
    """M6: durable_http no longer raises at build (the Runner wires the real per-run
    handler via set_permission_handler). Until wired it resolves to a fail-closed
    placeholder that DENIES every consult — never auto-allow."""
    import asyncio

    from warden.seams.defer import UnwiredDurableHandler

    h = build_permission_handler(PermissionsConfig(handler="durable_http"))
    assert isinstance(h, UnwiredDurableHandler)
    decision = asyncio.run(h.request_permission("Write", {"path": "x"}, "reason"))
    assert decision.allowed is False
    assert decision.source == "durable-unwired"


# --- tool scope ---------------------------------------------------------------


def test_tool_scope_none_when_unset():
    assert build_tool_scope(PermissionsConfig()) is None


def test_tool_scope_allowed():
    scope = build_tool_scope(PermissionsConfig(allowed_tools=["Read", "Grep"]))
    assert scope is not None
    assert scope.is_allowed("Read")
    assert not scope.is_allowed("Bash")


def test_tool_scope_denied():
    scope = build_tool_scope(PermissionsConfig(denied_tools=["Bash"]))
    assert scope is not None
    assert not scope.is_allowed("Bash")
    assert scope.is_allowed("Read")


# --- middleware ---------------------------------------------------------------


def test_middleware_names_resolve():
    from warden.safety.middleware.input.sanitize import SanitizeMiddleware

    input_mw, output_mw = build_middleware(
        MiddlewareConfig(input=["sanitize"], enable_input_middleware=True),
        SafetyConfig(),
    )
    assert len(input_mw) == 1
    assert isinstance(input_mw[0], SanitizeMiddleware)
    assert output_mw == []


def test_middleware_fuzzy_threshold_threaded_from_safety():
    from warden.config.models import ClassifierSettings

    safety = SafetyConfig(classifiers=ClassifierSettings(fuzzy_threshold=0.9))
    input_mw, _ = build_middleware(
        MiddlewareConfig(input=["fuzzy-intent"], enable_input_middleware=True),
        safety,
    )
    assert input_mw[0]._threshold == 0.9


def test_middleware_instances_appended_after_names():
    sentinel = object()
    input_mw, _ = build_middleware(
        MiddlewareConfig(
            input=["sanitize"],
            input_instances=[sentinel],
            enable_input_middleware=True,
        ),
        SafetyConfig(),
    )
    assert input_mw[-1] is sentinel
    assert len(input_mw) == 2


def test_middleware_unknown_input_name_raises():
    # §4b: the input NAME field is a typed allow-list (Literal), so an unknown
    # name is a pydantic ValidationError at config CONSTRUCTION — declared and
    # discoverable, before the builder ever runs.
    with pytest.raises(ValidationError):
        MiddlewareConfig(input=["nope"], enable_input_middleware=True)


def test_middleware_unknown_input_name_build_fallback_raises():
    # §4b defensive fallback: even if a raw ``str`` bypasses the Literal
    # allow-list (e.g. field mutated post-construction), the builder still
    # fail-fasts rather than silently no-op'ing.
    mw = MiddlewareConfig(enable_input_middleware=True)
    mw.input = ["nope"]  # bypass the construction-time allow-list
    with pytest.raises(ValueError, match="unknown input middleware"):
        build_middleware(mw, SafetyConfig())


def test_middleware_output_instances_pass_through_when_enabled():
    # M4 3b-2 §4a: output_instances flow through ONLY when the master switch is on.
    sentinel = object()
    _, output_mw = build_middleware(
        MiddlewareConfig(
            output_instances=[sentinel], enable_output_middleware=True
        ),
        SafetyConfig(),
    )
    assert output_mw == [sentinel]


def test_middleware_output_switch_off_is_hard_noop():
    # §4a: switch OFF ⇒ empty output pipeline REGARDLESS of configured instances.
    sentinel = object()
    _, output_mw = build_middleware(
        MiddlewareConfig(output_instances=[sentinel]),  # switch defaults False
        SafetyConfig(),
    )
    assert output_mw == []


def test_middleware_output_name_resolves_no_reject():
    # SAFE-5 / 3d: the declarative-output hard-reject is GONE — output names now
    # resolve through the output registry, symmetric to input. (Fail-first: the
    # pre-3d builder raised "declarative output middleware names are not
    # supported yet" for exactly this input.)
    from warden.safety.middleware.output.middleware import (
        StreamingLeakFilterMiddleware,
    )

    _, output_mw = build_middleware(
        MiddlewareConfig(output=["leak-filter"], enable_output_middleware=True),
        SafetyConfig(),
    )
    assert len(output_mw) == 1
    assert isinstance(output_mw[0], StreamingLeakFilterMiddleware)


def test_middleware_output_redact_name_resolves():
    from warden.safety.middleware.output.middleware import (
        RedactOutputMiddleware,
    )

    _, output_mw = build_middleware(
        MiddlewareConfig(output=["redact"], enable_output_middleware=True),
        SafetyConfig(),
    )
    assert isinstance(output_mw[0], RedactOutputMiddleware)


def test_middleware_output_name_threads_streaming_buffer_size():
    # The output registry merges the safety classifier knob, symmetric to input.
    from warden.config.models import ClassifierSettings

    safety = SafetyConfig(
        classifiers=ClassifierSettings(streaming_buffer_size=17)
    )
    _, output_mw = build_middleware(
        MiddlewareConfig(output=["leak-filter"], enable_output_middleware=True),
        safety,
    )
    assert output_mw[0]._buffer_size == 17


def test_middleware_output_names_before_instances():
    # Names resolve first, then bespoke output_instances append (symmetric input).
    sentinel = object()
    _, output_mw = build_middleware(
        MiddlewareConfig(
            output=["redact"],
            output_instances=[sentinel],
            enable_output_middleware=True,
        ),
        SafetyConfig(),
    )
    assert output_mw[-1] is sentinel
    assert len(output_mw) == 2


def test_middleware_unknown_output_name_rejected_construction():
    # §4b: output NAME field is a typed allow-list — unknown name is a
    # construction-time ValidationError.
    with pytest.raises(ValidationError):
        MiddlewareConfig(output=["nope"], enable_output_middleware=True)


def test_middleware_unknown_output_name_build_fallback_raises():
    # §4b defensive fallback: a raw-str bypass still fail-fasts at build.
    mw = MiddlewareConfig(enable_output_middleware=True)
    mw.output = ["nope"]  # bypass the construction-time allow-list
    with pytest.raises(ValueError, match="unknown output middleware"):
        build_middleware(mw, SafetyConfig())


# --- apply_workflow_middleware (SAFE-5: policy travels with the manifest) ------


def test_apply_workflow_middleware_none_is_passthrough():
    base = MiddlewareConfig(input=["sanitize"], enable_input_middleware=True)
    assert apply_workflow_middleware(base, None) is base


def test_apply_workflow_middleware_extends_and_overrides():
    from warden.workspace.workflow import WorkflowMiddleware

    base = MiddlewareConfig(input=["sanitize"])
    wf = WorkflowMiddleware(
        input=["cascade"], output=["redact"], enable_input=True, enable_output=True
    )
    eff = apply_workflow_middleware(base, wf)
    # names EXTEND the base lists...
    assert eff.input == ["sanitize", "cascade"]
    assert eff.output == ["redact"]
    # ...toggles OVERRIDE when set...
    assert eff.enable_input_middleware is True
    assert eff.enable_output_middleware is True
    # ...and the base config is NEVER mutated in place.
    assert base.input == ["sanitize"]
    assert base.enable_input_middleware is False


def test_apply_workflow_middleware_toggle_none_keeps_base():
    from warden.workspace.workflow import WorkflowMiddleware

    base = MiddlewareConfig(enable_output_middleware=True)
    # enable_output unset (None) → base switch (True) is preserved.
    eff = apply_workflow_middleware(base, WorkflowMiddleware(output=["redact"]))
    assert eff.enable_output_middleware is True


def test_apply_workflow_middleware_result_builds_expected_pipelines():
    from warden.safety.middleware.input.cascade import CascadeMiddleware
    from warden.safety.middleware.output.middleware import (
        RedactOutputMiddleware,
    )
    from warden.workspace.workflow import WorkflowMiddleware

    base = MiddlewareConfig()
    wf = WorkflowMiddleware(
        input=["cascade"], output=["redact"], enable_input=True, enable_output=True
    )
    eff = apply_workflow_middleware(base, wf)
    input_mw, output_mw = build_middleware(eff, SafetyConfig())
    assert any(isinstance(m, CascadeMiddleware) for m in input_mw)
    assert any(isinstance(m, RedactOutputMiddleware) for m in output_mw)


def test_middleware_input_switch_off_is_hard_noop():
    # §4a: INPUT switch OFF ⇒ empty input pipeline REGARDLESS of a listed name.
    input_mw, _ = build_middleware(
        MiddlewareConfig(input=["sanitize"], enable_input_middleware=False),
        SafetyConfig(),
    )
    assert input_mw == []


def test_middleware_cascade_builds_default_battery():
    # §4c: input=["cascade"] resolves the config-declared production cascade —
    # default members regex-input (no allow authority) → llm-judge (authority).
    from warden.safety.middleware.input.cascade import CascadeMiddleware

    input_mw, _ = build_middleware(
        MiddlewareConfig(input=["cascade"], enable_input_middleware=True),
        SafetyConfig(),
    )
    assert len(input_mw) == 1
    cascade = input_mw[0]
    assert isinstance(cascade, CascadeMiddleware)
    # default thresholds from CascadeConfig.
    assert cascade._block_threshold == 0.5
    assert cascade._allow_threshold == 0.8
    assert cascade._default_allow is True
    # two ordered stages: cheap regex (not authority) then judge (authority).
    assert len(cascade._stages) == 2
    assert cascade._stages[0].allow_authority is False
    assert cascade._stages[1].allow_authority is True


def test_middleware_cascade_unknown_member_raises():
    # fail-fast (LAW 7): an unknown battery name in the cascade errors at build.
    from warden.config.models import CascadeConfig

    safety = SafetyConfig(cascade=CascadeConfig(members=["nope"]))
    with pytest.raises(ValueError, match="unknown cascade classifier battery"):
        build_middleware(
            MiddlewareConfig(input=["cascade"], enable_input_middleware=True),
            safety,
        )


def test_safety_config_structural_absence_deleted():
    # The dead flag is gone (read nowhere).
    assert not hasattr(SafetyConfig(), "structural_absence")


# --- persistence --------------------------------------------------------------


def test_persistence_local_backend():
    from warden.persistence.local_backend import LocalFileBackend

    cfg, backend = build_persistence(
        PersistenceConfig(state_root="data/store"),
        WorkspaceConfig(base_dir="data/workspaces"),
    )
    assert str(cfg.base_dir) == "data/workspaces"
    assert str(cfg.state_root) == "data/store"
    assert cfg.prefix == "v1"
    assert isinstance(backend, LocalFileBackend)


def test_persistence_exclude_patterns_flow_through():
    cfg, _ = build_persistence(
        PersistenceConfig(exclude_patterns=("foo/",)),
        WorkspaceConfig(),
    )
    assert cfg.exclude_patterns == ("foo/",)


def test_persistence_s3_requires_bucket():
    with pytest.raises(ValueError, match="requires persistence.s3.bucket"):
        build_persistence(
            PersistenceConfig(backend="s3"),
            WorkspaceConfig(),
        )


def test_persistence_s3_backend():
    from warden.persistence.s3_backend import S3Boto3Backend

    _, backend = build_persistence(
        PersistenceConfig(backend="s3", s3=S3Config(bucket="my-bucket", prefix="p")),
        WorkspaceConfig(),
    )
    assert isinstance(backend, S3Boto3Backend)


def test_persistence_s3_config_slice_threaded_to_backend():
    """C7 (M8): build_persistence threads the FULL S3Config slice into the
    backend — endpoint, region, and the access-key pair — so the backend never
    reaches for get_harness_settings() itself. bucket_location wins over region.
    """
    _, backend = build_persistence(
        PersistenceConfig(
            backend="s3",
            s3=S3Config(
                bucket="my-bucket",
                endpoint="http://minio:9000",
                region="us-east-1",
                bucket_location="ap-south-1",
                access_key_id="std-id",
                access_key="nonstd-id",
                secret_access_key="nonstd-secret",
            ),
        ),
        WorkspaceConfig(),
    )
    assert backend._endpoint_url == "http://minio:9000"
    # bucket_location wins over region (matches the prior settings precedence).
    assert backend._region == "ap-south-1"
    assert backend._access_key_id == "std-id"
    assert backend._access_key == "nonstd-id"
    assert backend._secret_access_key == "nonstd-secret"


# --- provider -----------------------------------------------------------------


def test_resolve_model_explicit_wins():
    assert resolve_model(ProviderConfig(provider="codex", model="gpt-x")) == "gpt-x"


def test_resolve_model_codex_default():
    assert resolve_model(ProviderConfig(provider="codex")) == "gpt-5.4"


def test_resolve_model_openharness_default():
    assert resolve_model(ProviderConfig(provider="openharness")) == "qwen3:1.7b"


def test_resolve_model_claude_none():
    assert resolve_model(ProviderConfig(provider="claude")) is None
