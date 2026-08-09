"""Tests for workspace.workflow.loader — mtime-cached workflow loading."""

import time
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


VALID_YAML = """\
name: study
description: Study a repo
permissions:
  mode: read_only
  file_access:
    read: ["src/**"]
    write: ["docs/**"]
  tool_access:
    allow: [Read, Grep]
    deny: [Bash]
"""


def test_load_valid_workflow(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)
    wf = load_workflow(tmp_path, "study")
    assert wf is not None
    assert wf.name == "study"
    assert wf.permissions is not None
    assert wf.permissions.mode == "read_only"


def test_missing_workflow_returns_none(tmp_path: Path):
    result = load_workflow(tmp_path, "nonexistent")
    assert result is None


def test_malformed_yaml_raises(tmp_path: Path):
    """Present-but-broken YAML fails closed: WorkflowLoadError."""
    _write_yaml(tmp_path, "bad", "{{{{not: valid: yaml:")
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "bad")


def test_mtime_cache_hit(tmp_path: Path):
    _write_yaml(tmp_path, "cached", VALID_YAML)
    wf1 = load_workflow(tmp_path, "cached")
    wf2 = load_workflow(tmp_path, "cached")
    assert wf1 is wf2  # same object — cache hit


def test_mtime_cache_invalidation(tmp_path: Path):
    p = _write_yaml(tmp_path, "cached", VALID_YAML)
    wf1 = load_workflow(tmp_path, "cached")

    # Bump mtime to force cache invalidation
    time.sleep(0.05)
    p.write_text(VALID_YAML.replace("Study a repo", "Updated description"))

    wf2 = load_workflow(tmp_path, "cached")
    assert wf2 is not None
    assert wf2 is not wf1
    assert wf2.description == "Updated description"


def test_permissions_parsed(tmp_path: Path):
    _write_yaml(tmp_path, "full", VALID_YAML)
    wf = load_workflow(tmp_path, "full")
    assert wf is not None

    # Permissions
    assert wf.permissions is not None
    assert wf.permissions.file_access is not None
    assert wf.permissions.file_access.read == ["src/**"]
    assert wf.permissions.tool_access is not None
    assert wf.permissions.tool_access.deny == ["Bash"]


# --- middleware block (SAFE-5: safety policy travels with the manifest) ---

MIDDLEWARE_YAML = """\
name: guarded
description: A guarded workflow
middleware:
  input: [cascade]
  output: [redact]
  enable_input: true
  enable_output: true
"""


def test_workflow_middleware_block_parses(tmp_path: Path):
    _write_yaml(tmp_path, "guarded", MIDDLEWARE_YAML)
    wf = load_workflow(tmp_path, "guarded")
    assert wf is not None
    assert wf.middleware is not None
    assert wf.middleware.input == ["cascade"]
    assert wf.middleware.output == ["redact"]
    assert wf.middleware.enable_input is True
    assert wf.middleware.enable_output is True


def test_workflow_without_middleware_block_is_none(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)  # no middleware: block
    wf = load_workflow(tmp_path, "study")
    assert wf is not None
    assert wf.middleware is None


def test_workflow_middleware_bad_name_fails_closed(tmp_path: Path):
    # A middleware name outside the §4b allow-list → schema-invalid → fail-closed.
    _write_yaml(
        tmp_path,
        "bad_mw",
        "name: bad_mw\ndescription: x\nmiddleware:\n  input: [not-a-real-name]\n",
    )
    with pytest.raises(WorkflowLoadError):
        load_workflow(tmp_path, "bad_mw")


def test_workflow_middleware_travels_through_build(tmp_path: Path):
    """End-to-end: a YAML middleware block → Workflow → merged config → pipelines."""
    from warden.config.build import (
        apply_workflow_middleware,
        build_middleware,
    )
    from warden.config.models import MiddlewareConfig, SafetyConfig
    from warden.safety.middleware.input.cascade import CascadeMiddleware
    from warden.safety.middleware.output.middleware import (
        RedactOutputMiddleware,
    )

    _write_yaml(tmp_path, "guarded", MIDDLEWARE_YAML)
    wf = load_workflow(tmp_path, "guarded")
    assert wf is not None

    eff = apply_workflow_middleware(MiddlewareConfig(), wf.middleware)
    input_mw, output_mw = build_middleware(eff, SafetyConfig())
    assert any(isinstance(m, CascadeMiddleware) for m in input_mw)
    assert any(isinstance(m, RedactOutputMiddleware) for m in output_mw)


# --- compute_deny_baseline ---

def test_deny_baseline_no_workflows_dir(tmp_path: Path):
    assert compute_deny_baseline(tmp_path) == []


def test_deny_baseline_empty_workflows_dir(tmp_path: Path):
    (tmp_path / ".workflows").mkdir()
    assert compute_deny_baseline(tmp_path) == []


def test_deny_baseline_single_workflow(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)  # denies Bash
    result = compute_deny_baseline(tmp_path)
    assert result == ["Bash"]


def test_deny_baseline_intersection_of_two(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)  # denies Bash
    _write_yaml(tmp_path, "build", """\
name: build
description: Build workflow
capabilities: []
permissions:
  mode: auto
  tool_access:
    deny: [Bash, Write]
""")
    result = compute_deny_baseline(tmp_path)
    assert result == ["Bash"]  # intersection: both deny Bash


def test_deny_baseline_empty_when_workflow_has_no_deny(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)  # denies Bash
    _write_yaml(tmp_path, "open", """\
name: open
description: Open workflow
capabilities: []
permissions:
  mode: auto
""")
    result = compute_deny_baseline(tmp_path)
    assert result == []  # open has no deny → intersection empty


def test_deny_baseline_excludes_workspace_yaml(tmp_path: Path):
    _write_yaml(tmp_path, "study", VALID_YAML)  # denies Bash
    _write_yaml(tmp_path, "workspace", """\
name: workspace
description: Workspace config
capabilities: []
permissions:
  mode: auto
""")
    result = compute_deny_baseline(tmp_path)
    assert result == ["Bash"]  # workspace.yaml excluded, only study counts
