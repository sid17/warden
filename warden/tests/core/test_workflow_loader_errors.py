"""Error / fail-CLOSED path tests for workspace.workflow.loader.

Complements test_workflow_loader.py (happy path + cache). Here we pin down
the loader's two distinct outcomes and, critically, the fail-CLOSED behavior
of compute_deny_baseline:

  - MISSING file  -> load_workflow returns None (no-op; not every workflow exists)
  - PRESENT but BROKEN file (malformed YAML / not a mapping / schema-invalid)
    -> load_workflow raises WorkflowLoadError, which propagates through
       compute_deny_baseline to HARD-STOP session creation.

A broken deny-rule file must not silently open the agent up. These tests
document that security-relevant choice so a regression is caught.
"""

import logging
from pathlib import Path

import pytest

from warden.workspace.workflow.loader import (
    WorkflowLoadError,
    clear_cache,
    compute_deny_baseline,
    load_workflow,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    wf_dir = tmp_path / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    p = wf_dir / f"{name}.yaml"
    p.write_text(content)
    return p


VALID_DENY_YAML = """\
name: study
description: Study a repo
capabilities: []
permissions:
  mode: read_only
  tool_access:
    deny: [Bash]
"""


# --- EXT-P1/A2 (E4): event_tool_map load-validation (fail-closed) ---


def test_event_tool_map_valid_values_load(tmp_path: Path):
    p = _write_yaml(tmp_path, "authoring", (
        "name: authoring\ndescription: d\n"
        "event_tool_map:\n  emit_checkpoint: checkpoint\n  course_complete: completion\n"
    ))
    wf = load_workflow(p.parent.parent, "authoring")
    assert wf.event_tool_map == {"emit_checkpoint": "checkpoint",
                                 "course_complete": "completion"}


def test_event_tool_map_unknown_value_raises(tmp_path: Path):
    _write_yaml(tmp_path, "bad", (
        "name: bad\ndescription: d\n"
        "event_tool_map:\n  foo: bogus_type\n"
    ))
    with pytest.raises(WorkflowLoadError, match="not a known event type"):
        load_workflow(tmp_path, "bad")


def test_apply_workflow_event_map_merges_and_is_pure():
    from warden.config.build import apply_workflow_event_map
    from warden.config.models import CustomToolsConfig

    base = CustomToolsConfig(event_tool_map={"a": "checkpoint"})
    merged = apply_workflow_event_map(base, {"b": "completion"})
    assert merged.event_tool_map == {"a": "checkpoint", "b": "completion"}
    assert base.event_tool_map == {"a": "checkpoint"}  # base untouched
    # None/empty → passthrough (same object).
    assert apply_workflow_event_map(base, None) is base


# --- load_workflow: MISSING file → None (no-op) ---


def test_missing_workflows_dir_returns_none(tmp_path: Path):
    """No .workflows dir at all → wf_path.is_file() is False → None (missing)."""
    assert load_workflow(tmp_path, "study") is None


def test_missing_file_in_existing_dir_returns_none(tmp_path: Path):
    """.workflows exists but the named yaml doesn't → None (missing)."""
    (tmp_path / ".workflows").mkdir()
    assert load_workflow(tmp_path, "nonexistent") is None


def test_path_is_directory_not_file_returns_none(tmp_path: Path):
    """A directory named like the workflow file is not a file → None (missing)."""
    wf_dir = tmp_path / ".workflows"
    wf_dir.mkdir()
    (wf_dir / "study.yaml").mkdir()  # directory, not a file
    assert load_workflow(tmp_path, "study") is None


# --- load_workflow: PRESENT-but-BROKEN file → raise WorkflowLoadError ---


def test_malformed_yaml_raises(tmp_path: Path):
    """Unparseable YAML raises YAMLError → WorkflowLoadError (present-but-broken)."""
    _write_yaml(tmp_path, "bad", "key: [unclosed, list\n  : : :")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "bad")


def test_yaml_tab_indentation_raises(tmp_path: Path):
    """Tabs in indentation are a YAML error → WorkflowLoadError."""
    _write_yaml(tmp_path, "tabs", "name: x\n\tdescription: y\n")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "tabs")


def test_yaml_scalar_not_dict_raises(tmp_path: Path):
    """Valid YAML that parses to a bare scalar isn't a dict → WorkflowLoadError."""
    _write_yaml(tmp_path, "scalar", "just-a-string")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "scalar")


def test_yaml_list_not_dict_raises(tmp_path: Path):
    """Valid YAML that parses to a list isn't a dict → WorkflowLoadError."""
    _write_yaml(tmp_path, "list", "- one\n- two\n")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "list")


def test_empty_file_raises(tmp_path: Path):
    """An empty file yaml.safe_load()s to None (not a dict) → WorkflowLoadError."""
    _write_yaml(tmp_path, "empty", "")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "empty")


def test_broken_yaml_logs_error(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """The raise point logs an ERROR naming the file path and the reason."""
    p = _write_yaml(tmp_path, "scalar", "just-a-string")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(WorkflowLoadError):
            load_workflow(tmp_path, "scalar")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log at the raise point, got none"
    messages = " ".join(r.getMessage() for r in errors)
    assert str(p) in messages, f"expected file path {p} in ERROR log; got: {messages}"


def test_validation_error_wrong_type_for_permissions_raises(tmp_path: Path):
    """permissions given as a scalar instead of a mapping → ValidationError → raise."""
    _write_yaml(
        tmp_path,
        "badperms",
        """\
name: badperms
description: Permissions is a string
capabilities: []
permissions: "not-a-mapping"
""",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "badperms")


# --- compute_deny_baseline: FAIL-CLOSED behavior when a workflow can't load ---


def test_deny_baseline_broken_workflow_hard_stops(tmp_path: Path):
    """A broken workflow HARD-STOPS the baseline — it does NOT silently apply
    only the valid workflow's deny set.

    FAIL-CLOSED: the broken deny-rule file is fatal. A valid `study` alongside
    it does not rescue the computation; the error propagates.
    """
    _write_yaml(tmp_path, "study", VALID_DENY_YAML)  # denies Bash
    _write_yaml(tmp_path, "broken", "just-a-string")  # load_workflow raises
    with pytest.raises(WorkflowLoadError):
        compute_deny_baseline(tmp_path)


def test_deny_baseline_all_workflows_broken_hard_stops(tmp_path: Path):
    """SAFETY-CRITICAL FAIL-CLOSED: if any workflow fails to load, the baseline
    computation raises rather than returning an empty (permissive) deny list.

    This pins the fail-closed: broken/malformed workflow config must NOT open
    the agent up. A regression toward fail-open (returning []) would change this.
    """
    _write_yaml(tmp_path, "broken1", "just-a-string")
    _write_yaml(tmp_path, "broken2", "- a\n- b\n")
    _write_yaml(tmp_path, "broken3", "key: [unclosed")
    with pytest.raises(WorkflowLoadError):
        compute_deny_baseline(tmp_path)


def test_deny_baseline_missing_dir_denies_nothing(tmp_path: Path):
    """No .workflows dir → no-op empty deny list (missing is not an error)."""
    assert compute_deny_baseline(tmp_path) == []


def test_deny_baseline_dir_with_only_broken_and_workspace_hard_stops(tmp_path: Path):
    """workspace.yaml is excluded; the only real workflow is broken → the broken
    file hard-stops the computation (fail-closed)."""
    _write_yaml(tmp_path, "workspace", VALID_DENY_YAML)  # excluded by stem filter
    _write_yaml(tmp_path, "broken", "just-a-string")  # present-but-broken → raise
    with pytest.raises(WorkflowLoadError):
        compute_deny_baseline(tmp_path)


# --- VISIBILITY: the hard-stop must be logged as an ERROR naming the file ---


def test_deny_baseline_broken_workflow_logs_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A broken workflow that hard-stops the baseline must produce an ERROR log
    naming the offending file so it's discoverable."""
    _write_yaml(tmp_path, "study", VALID_DENY_YAML)  # loads, denies Bash
    p = _write_yaml(tmp_path, "broken", "just-a-string")  # load_workflow raises

    with caplog.at_level(logging.ERROR):
        with pytest.raises(WorkflowLoadError):
            compute_deny_baseline(tmp_path)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR when a workflow is broken, got none"
    assert any(str(p) in r.getMessage() for r in errors), (
        f"expected the broken workflow path {p} named in an ERROR; "
        f"got messages: {[r.getMessage() for r in errors]}"
    )


def test_deny_baseline_broken_alongside_no_deny_workflow_hard_stops(tmp_path: Path):
    """A broken file is fatal even when co-located with a loadable no-deny
    workflow — the broken file raises before any intersection is returned."""
    _write_yaml(tmp_path, "study", VALID_DENY_YAML)  # denies Bash
    _write_yaml(tmp_path, "broken", "- not - a - dict")  # present-but-broken → raise
    _write_yaml(
        tmp_path,
        "open",
        """\
name: open
description: No deny list
capabilities: []
permissions:
  mode: auto
""",
    )
    with pytest.raises(WorkflowLoadError):
        compute_deny_baseline(tmp_path)
