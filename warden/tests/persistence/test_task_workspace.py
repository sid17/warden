"""Tests for workspace.task_workspace — caller-facing glue.

Uses LocalFileBackend + tmp_path with no real CLI. Async methods are driven with
asyncio.run(...) inside sync tests, matching the repo convention (no
pytest-asyncio configured).
"""

import asyncio
import json
from pathlib import Path

import pytest

from warden.persistence.config import PersistenceConfig
from warden.persistence.keys import task_dir
from warden.persistence.local_backend import LocalFileBackend
from warden.workspace.task_workspace import (
    ensure_restored,
    home_env,
    snapshot,
)

USER = "default"
TASK = "task_1"


def _cfg(tmp_path: Path) -> PersistenceConfig:
    return PersistenceConfig(
        base_dir=tmp_path / "workspaces",
        state_root=tmp_path / "store",
    )


def _backend(cfg: PersistenceConfig) -> LocalFileBackend:
    return LocalFileBackend(cfg.state_root, exclude_patterns=cfg.exclude_patterns)


# --- home_env -------------------------------------------------------------


def test_home_env_claude_cli_sets_config_dir(tmp_path):
    td = tmp_path / "task"
    env = home_env(td, "claude-cli")
    assert env["CLAUDE_CONFIG_DIR"] == str(td / ".claude-home")
    assert "CODEX_HOME" not in env


def test_home_env_claude_sets_config_dir(tmp_path):
    td = tmp_path / "task"
    env = home_env(td, "claude")
    assert env["CLAUDE_CONFIG_DIR"] == str(td / ".claude-home")


def test_home_env_codex_sets_codex_home(tmp_path):
    td = tmp_path / "task"
    env = home_env(td, "codex")
    assert env["CODEX_HOME"] == str(td / ".codex")
    assert "CLAUDE_CONFIG_DIR" not in env


def test_home_env_openharness_no_relocation(tmp_path):
    td = tmp_path / "task"
    env = home_env(td, "openharness")
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CODEX_HOME" not in env


def test_home_env_claude_token_passthrough(tmp_path, monkeypatch):
    td = tmp_path / "task"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    env = home_env(td, "claude-cli")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"


def test_home_env_claude_token_absent(tmp_path, monkeypatch):
    td = tmp_path / "task"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = home_env(td, "claude-cli")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # Home var is still present; only the (absent) tokens are omitted.
    assert env["CLAUDE_CONFIG_DIR"] == str(td / ".claude-home")


def test_home_env_codex_token_passthrough(tmp_path, monkeypatch):
    td = tmp_path / "task"
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    env = home_env(td, "codex")
    assert env["OPENAI_API_KEY"] == "openai-key"


def test_home_env_auth_env_overrides_os_environ(tmp_path, monkeypatch):
    """A per-run auth_env fully replaces os.environ as the credential source —
    the operator's key is not consulted (per-user managed-key path)."""
    td = tmp_path / "task"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-key")
    env = home_env(td, "claude-cli", auth_env={"ANTHROPIC_API_KEY": "user-42-key"})
    # Only the supplied key resolves; the operator's os.environ creds are ignored.
    assert env["ANTHROPIC_API_KEY"] == "user-42-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == str(td / ".claude-home")


def test_home_env_does_not_mutate_os_environ(tmp_path):
    import os

    td = tmp_path / "task"
    before = dict(os.environ)
    home_env(td, "claude-cli")
    assert dict(os.environ) == before


# --- ensure_restored (guarded) --------------------------------------------


def test_ensure_restored_nothing_backed_up_returns_uncreated_path(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)

    td = asyncio.run(ensure_restored(cfg, backend, USER, TASK))
    assert td == task_dir(cfg.base_dir, USER, TASK)
    # Caller bootstraps fresh: ensure_restored must NOT create the folder.
    assert not td.exists()


def test_ensure_restored_restores_and_writes_marker(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)

    td = task_dir(cfg.base_dir, USER, TASK)
    (td).mkdir(parents=True)
    (td / "COUNT.txt").write_text("1 2 3")

    stats = asyncio.run(snapshot(cfg, backend, USER, TASK))
    assert stats["files"] >= 1

    # Simulate a fresh machine: delete the local folder entirely.
    import shutil

    shutil.rmtree(td)
    assert not td.exists()

    restored = asyncio.run(ensure_restored(cfg, backend, USER, TASK))
    assert restored == td
    assert (td / "COUNT.txt").read_text() == "1 2 3"

    marker = td / ".workspace" / "restored.json"
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["key"].endswith(f"{USER}/{TASK}.tar.gz")
    assert "restored_at" in data
    assert data["files"] >= 1


def test_ensure_restored_noop_when_marker_present(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)

    td = task_dir(cfg.base_dir, USER, TASK)
    td.mkdir(parents=True)
    (td / "COUNT.txt").write_text("original")
    asyncio.run(snapshot(cfg, backend, USER, TASK))

    # Restore once to establish the marker.
    import shutil

    shutil.rmtree(td)
    asyncio.run(ensure_restored(cfg, backend, USER, TASK))
    assert (td / ".workspace" / "restored.json").is_file()

    # Back up DIFFERENT content under the same key, then mutate local, then call
    # ensure_restored again. Because td + marker exist it must be a no-op: it must
    # NOT overwrite the local content with the store archive.
    (td / "COUNT.txt").write_text("changed-in-store")
    asyncio.run(snapshot(cfg, backend, USER, TASK))
    (td / "COUNT.txt").write_text("local-current")

    result = asyncio.run(ensure_restored(cfg, backend, USER, TASK))
    assert result == td
    assert (td / "COUNT.txt").read_text() == "local-current"


# --- snapshot -------------------------------------------------------------


def test_snapshot_missing_folder_raises(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)
    with pytest.raises(FileNotFoundError):
        asyncio.run(snapshot(cfg, backend, USER, TASK))


def test_snapshot_restore_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)

    td = task_dir(cfg.base_dir, USER, TASK)
    td.mkdir(parents=True)
    (td / "sub").mkdir()
    (td / "sub" / "data.bin").write_bytes(b"\x00\x01\x02hello")

    asyncio.run(snapshot(cfg, backend, USER, TASK))

    import shutil

    shutil.rmtree(td)
    asyncio.run(ensure_restored(cfg, backend, USER, TASK))

    assert (td / "sub" / "data.bin").read_bytes() == b"\x00\x01\x02hello"


def test_end_to_end_snapshot_delete_restore(tmp_path):
    cfg = _cfg(tmp_path)
    backend = _backend(cfg)

    td = task_dir(cfg.base_dir, USER, TASK)
    td.mkdir(parents=True)
    payload = b"count: 1 2 3 4 5 6 7 8 9 10\n"
    (td / "COUNT.txt").write_bytes(payload)

    asyncio.run(snapshot(cfg, backend, USER, TASK))

    import shutil

    shutil.rmtree(td)
    assert not td.exists()

    restored = asyncio.run(ensure_restored(cfg, backend, USER, TASK))
    assert (restored / "COUNT.txt").read_bytes() == payload
    assert (restored / ".workspace" / "restored.json").is_file()
