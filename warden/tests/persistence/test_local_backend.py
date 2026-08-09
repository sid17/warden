"""Tests for persistence.local_backend — LocalFileBackend.

Uses a real filesystem via tmp_path (no mocking needed). Async methods are
driven with asyncio.run(...) inside sync tests, matching the repo's existing
convention (no pytest-asyncio configured).
"""

import asyncio
from pathlib import Path

import pytest

from warden.persistence.backend import WorkspaceRestoreError
from warden.persistence.local_backend import LocalFileBackend

KEY = "v1/default/task_1.tar.gz"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_workspace(root: Path) -> Path:
    ws = root / "ws"
    _write(ws / "COUNT.txt", "1 2 3")
    _write(ws / ".claude" / "settings.json", "{}")
    _write(ws / "src" / "main.py", "print('hi')")
    return ws


def test_backup_restore_roundtrip_preserves_contents(tmp_path):
    ws = _make_workspace(tmp_path)
    backend = LocalFileBackend(tmp_path / "store")

    stats = asyncio.run(backend.backup(ws, KEY))
    assert stats["key"] == KEY
    assert stats["files"] == 3
    assert stats["bytes"] > 0

    out = tmp_path / "restored"
    rstats = asyncio.run(backend.restore(KEY, out))
    assert rstats["files"] == 3

    assert (out / "COUNT.txt").read_text() == "1 2 3"
    assert (out / ".claude" / "settings.json").read_text() == "{}"
    assert (out / "src" / "main.py").read_text() == "print('hi')"


def test_exists_true_and_false(tmp_path):
    ws = _make_workspace(tmp_path)
    backend = LocalFileBackend(tmp_path / "store")

    assert asyncio.run(backend.exists(KEY)) is False
    asyncio.run(backend.backup(ws, KEY))
    assert asyncio.run(backend.exists(KEY)) is True


def test_second_backup_atomically_overwrites(tmp_path):
    ws = _make_workspace(tmp_path)
    backend = LocalFileBackend(tmp_path / "store")

    asyncio.run(backend.backup(ws, KEY))

    # Mutate the workspace, back up again -> archive reflects the new content.
    (ws / "COUNT.txt").write_text("10 11 12")
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))
    assert (out / "COUNT.txt").read_text() == "10 11 12"

    # No leftover tempfiles from the atomic-replace dance.
    store_dir = (tmp_path / "store" / "v1" / "default")
    leftovers = [p.name for p in store_dir.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_restore_missing_key_raises(tmp_path):
    backend = LocalFileBackend(tmp_path / "store")
    with pytest.raises(WorkspaceRestoreError) as excinfo:
        asyncio.run(backend.restore(KEY, tmp_path / "restored"))
    assert excinfo.value.key == KEY


def test_excludes_drop_node_modules_and_pycache(tmp_path):
    ws = tmp_path / "ws"
    _write(ws / "keep.py", "ok")
    _write(ws / "node_modules" / "dep" / "index.js", "junk")
    _write(ws / "pkg" / "__pycache__" / "mod.cpython.pyc", "bytecode")
    _write(ws / "stale.pyc", "bytecode")

    backend = LocalFileBackend(
        tmp_path / "store",
        exclude_patterns=("node_modules/", "__pycache__/", "*.pyc"),
    )
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))

    assert (out / "keep.py").exists()
    assert not (out / "node_modules").exists()
    assert not (out / "pkg" / "__pycache__").exists()
    assert not (out / "stale.pyc").exists()


# --- EXT-A1: confined single-file read from the snapshot ------------------


def test_read_file_returns_member_bytes(tmp_path):
    ws = tmp_path / "ws"
    _write(ws / "drafts" / "ch1.md", "# Chapter 1")
    backend = LocalFileBackend(tmp_path / "store")
    asyncio.run(backend.backup(ws, KEY))

    data = asyncio.run(backend.read_file(KEY, "drafts/ch1.md"))
    assert data == b"# Chapter 1"


def test_read_file_rejects_traversal_and_nul(tmp_path):
    ws = _make_workspace(tmp_path)
    backend = LocalFileBackend(tmp_path / "store")
    asyncio.run(backend.backup(ws, KEY))

    for bad in ("../../etc/passwd", "/etc/passwd", "a/../../b", "x\x00y", ""):
        with pytest.raises(ValueError):
            asyncio.run(backend.read_file(KEY, bad))


def test_read_file_missing_member_is_file_not_found(tmp_path):
    ws = _make_workspace(tmp_path)
    backend = LocalFileBackend(tmp_path / "store")
    asyncio.run(backend.backup(ws, KEY))

    with pytest.raises(FileNotFoundError):
        asyncio.run(backend.read_file(KEY, "does/not/exist.md"))


def test_read_file_missing_archive_is_file_not_found(tmp_path):
    backend = LocalFileBackend(tmp_path / "store")
    with pytest.raises(FileNotFoundError):
        asyncio.run(backend.read_file(KEY, "drafts/ch1.md"))


def test_read_file_directory_member_is_file_not_found(tmp_path):
    ws = tmp_path / "ws"
    _write(ws / "drafts" / "ch1.md", "x")
    backend = LocalFileBackend(tmp_path / "store")
    asyncio.run(backend.backup(ws, KEY))
    with pytest.raises(FileNotFoundError):
        asyncio.run(backend.read_file(KEY, "drafts"))  # a directory, not a file


def test_read_file_cannot_reach_excluded_credential(tmp_path):
    """Credentials are excluded from every snapshot, so read_file can never serve
    one (built-in safety — E1 §2)."""
    ws = tmp_path / "ws"
    _write(ws / "keep.md", "ok")
    _write(ws / ".codex" / "auth.json", "SECRET-TOKEN")
    backend = LocalFileBackend(tmp_path / "store")
    asyncio.run(backend.backup(ws, KEY))
    # The credential was dropped at archive time → not readable.
    with pytest.raises(FileNotFoundError):
        asyncio.run(backend.read_file(KEY, ".codex/auth.json"))


def test_git_dir_is_kept_even_with_excludes(tmp_path):
    ws = tmp_path / "ws"
    _write(ws / "file.txt", "data")
    _write(ws / ".git" / "HEAD", "ref: refs/heads/main")
    _write(ws / ".claude-home" / "token.json", "creds")
    _write(ws / ".codex" / "session.json", "sess")
    # Even a broad exclude set must not drop the resume-critical dirs.
    _write(ws / "node_modules" / "x.js", "junk")

    backend = LocalFileBackend(
        tmp_path / "store",
        exclude_patterns=("node_modules/", ".venv/", "__pycache__/", "*.pyc"),
    )
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))

    assert (out / ".git" / "HEAD").read_text() == "ref: refs/heads/main"
    assert (out / ".claude-home" / "token.json").exists()
    assert (out / ".codex" / "session.json").exists()
    assert not (out / "node_modules").exists()
