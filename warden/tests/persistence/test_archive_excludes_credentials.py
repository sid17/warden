"""Hermetic proof that credentials NEVER enter a workspace snapshot (A2).

The archive layer must drop provider credential files (``auth.json`` /
``.credentials.json``) that live inside a pinned provider home, even though the
provider home itself (``.codex`` / ``.claude-home``) is on the ``_ALWAYS_KEEP``
list. This inspects a *produced tarball* and asserts no credential is inside —
the belt half of the exclude+re-inject design (ADR credential-backup-separation).

No model, no backend creds — pure `write_tar_gz` over a temp dir.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from warden.persistence.archive import write_tar_gz
from warden.persistence.config import DEFAULT_EXCLUDE_PATTERNS


def _tar_basenames(tar_path: Path) -> list[str]:
    with tarfile.open(tar_path, "r:gz") as tar:
        return [Path(m.name).name for m in tar.getmembers()]


def _tar_member_names(tar_path: Path) -> list[str]:
    with tarfile.open(tar_path, "r:gz") as tar:
        return [m.name for m in tar.getmembers()]


def test_codex_auth_json_excluded_from_tarball(tmp_path: Path) -> None:
    """A planted ``.codex/auth.json`` must NOT appear in the produced tarball."""
    src = tmp_path / "task"
    codex = src / ".codex"
    (codex / "sessions").mkdir(parents=True)
    # The credential (must be dropped) …
    (codex / "auth.json").write_text('{"OPENAI_API_KEY":"sk-SECRET-TOKEN"}')
    (codex / "config.toml").write_text("model = 'gpt-5.4'\n")
    # … and the resume-critical transcript (must be kept).
    rollout = codex / "sessions" / "rollout-2026-abc.jsonl"
    rollout.write_text('{"turn": 1}\n')

    dest = tmp_path / "snap.tar.gz"
    write_tar_gz(src, dest, DEFAULT_EXCLUDE_PATTERNS)

    names = _tar_member_names(dest)
    basenames = _tar_basenames(dest)

    assert "auth.json" not in basenames, (
        f"credential leaked into the snapshot tarball: {names}"
    )
    # Belt: the raw secret bytes must not be in the archive either.
    with open(dest, "rb") as fh:
        blob = fh.read()
    assert b"sk-SECRET-TOKEN" not in blob, "raw OAuth token bytes leaked into tarball"

    # The transcript (memory) MUST survive — that's the whole point of the snapshot.
    assert any(n.endswith("rollout-2026-abc.jsonl") for n in names), (
        f"transcript was wrongly dropped: {names}"
    )
    # config.toml is NOT a secret — only auth.json is dropped. It travels in the
    # snapshot (and is also re-injected on bootstrap; the two are idempotent).
    assert "config.toml" in basenames


def test_claude_credentials_json_excluded(tmp_path: Path) -> None:
    """A ``.claude-home/.credentials.json`` must also be kept out of the tarball."""
    src = tmp_path / "task"
    home = src / ".claude-home"
    home.mkdir(parents=True)
    (home / ".credentials.json").write_text('{"token":"SECRET"}')
    (home / "history.jsonl").write_text('{"turn": 1}\n')

    dest = tmp_path / "snap.tar.gz"
    write_tar_gz(src, dest, DEFAULT_EXCLUDE_PATTERNS)

    basenames = _tar_basenames(dest)
    assert ".credentials.json" not in basenames
    assert "history.jsonl" in basenames  # non-secret provider state survives


def test_unrelated_auth_json_in_repo_is_kept(tmp_path: Path) -> None:
    """An ``auth.json`` NOT under a provider home is user data — keep it.

    The exclusion is scoped to pinned provider homes so we don't silently drop a
    user's own file that merely shares the name.
    """
    src = tmp_path / "task"
    (src / "myapp" / "config").mkdir(parents=True)
    (src / "myapp" / "config" / "auth.json").write_text('{"app":"data"}')

    dest = tmp_path / "snap.tar.gz"
    write_tar_gz(src, dest, DEFAULT_EXCLUDE_PATTERNS)

    names = _tar_member_names(dest)
    assert any(n.endswith("myapp/config/auth.json") for n in names), (
        f"a non-provider-home auth.json was wrongly dropped: {names}"
    )
