"""Tests for persistence.keys — key/path derivation + safety."""

from pathlib import Path

import pytest

from warden.persistence.config import PersistenceConfig
from warden.persistence.keys import (
    InvalidWorkspaceId,
    archive_key,
    task_dir,
    workspace_key,
)


def test_workspace_key_shape_and_default_prefix():
    assert workspace_key("default", "task_1") == "v1/default/task_1"


def test_workspace_key_custom_prefix():
    assert workspace_key("alice", "task_2", prefix="v2") == "v2/alice/task_2"


def test_workspace_key_human_ids_pass_through():
    # Human-friendly ids are untouched (no dash stripping forced on them).
    assert workspace_key("user-a", "task-42") == "v1/user-a/task-42"


def test_task_dir_shape():
    d = task_dir(Path("/data/workspaces"), "default", "task_1")
    assert d == Path("/data/workspaces/default/task_1")


def test_archive_key_appends_tar_gz():
    cfg = PersistenceConfig(
        base_dir=Path("/data/workspaces"), state_root=Path("/data/store")
    )
    assert archive_key(cfg, "default", "task_1") == "v1/default/task_1.tar.gz"


def test_archive_key_uses_cfg_prefix():
    cfg = PersistenceConfig(
        base_dir=Path("/w"), state_root=Path("/s"), prefix="v9"
    )
    assert archive_key(cfg, "u", "t") == "v9/u/t.tar.gz"


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "..", "../x", ".hidden", "", "  "])
def test_workspace_key_rejects_unsafe_ids(bad):
    with pytest.raises(InvalidWorkspaceId):
        workspace_key(bad, "task_1")
    with pytest.raises(InvalidWorkspaceId):
        workspace_key("default", bad)


def test_task_dir_rejects_unsafe_ids():
    with pytest.raises(InvalidWorkspaceId):
        task_dir(Path("/data"), "..", "task_1")
    with pytest.raises(InvalidWorkspaceId):
        task_dir(Path("/data"), "default", "a/b")
