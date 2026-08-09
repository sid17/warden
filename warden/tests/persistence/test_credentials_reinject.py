"""Hermetic tests for out-of-band credential re-injection on restore (A1).

``reinject_credentials`` re-hydrates ``<task>/.codex/auth.json`` from a read-only
out-of-band source AFTER restore — the only thing that authenticates a persisted
codex turn once the credential is excluded from the snapshot (A2). No model, no
network; we drive it with env vars + temp dirs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from warden.workspace.credentials import (
    codex_credential_source,
    reinject_credentials,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate from the host's real codex creds / API key for every test."""
    monkeypatch.delenv("WARDEN_CODEX_AUTH_SOURCE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Point ~ at an empty dir so the ~/.codex fallback can't pick up real host
    # creds and make the "no source" cases flaky.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(
        "warden.workspace.credentials.Path.home", lambda: fake_home
    )


def _make_source(tmp_path: Path, *, with_config: bool = True) -> Path:
    src = tmp_path / "mounted_codex"
    src.mkdir()
    (src / "auth.json").write_text('{"OPENAI_API_KEY":"sk-FROM-MOUNT"}')
    if with_config:
        (src / "config.toml").write_text("model = 'gpt-5.4'\n")
    return src


def test_reinject_copies_auth_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_source(tmp_path)
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(src))
    task = tmp_path / "task"
    task.mkdir()

    copied = reinject_credentials("codex", task)

    assert set(copied) == {"auth.json", "config.toml"}
    dst = task / ".codex" / "auth.json"
    assert dst.is_file()
    assert "sk-FROM-MOUNT" in dst.read_text()


def test_reinject_uses_ambient_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient CODEX_HOME (the mount) is the source when no explicit override."""
    src = _make_source(tmp_path, with_config=False)
    monkeypatch.setenv("CODEX_HOME", str(src))
    task = tmp_path / "task"
    task.mkdir()

    copied = reinject_credentials("codex", task)

    assert copied == ["auth.json"]
    assert (task / ".codex" / "auth.json").is_file()


def test_explicit_source_wins_over_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "auth.json").write_text('{"OPENAI_API_KEY":"sk-EXPLICIT"}')
    ambient = _make_source(tmp_path)  # has sk-FROM-MOUNT
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(explicit))
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    task = tmp_path / "task"
    task.mkdir()

    reinject_credentials("codex", task)

    assert "sk-EXPLICIT" in (task / ".codex" / "auth.json").read_text()


def test_openai_api_key_lane_skips_file_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With OPENAI_API_KEY set, codex uses the env var — no file needed."""
    src = _make_source(tmp_path)
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(src))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-lane")
    task = tmp_path / "task"
    task.mkdir()

    copied = reinject_credentials("codex", task)

    assert copied == []
    assert not (task / ".codex" / "auth.json").exists()


def test_non_codex_providers_are_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _make_source(tmp_path)
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(src))
    task = tmp_path / "task"
    task.mkdir()

    assert reinject_credentials("claude", task) == []
    assert reinject_credentials("openharness", task) == []
    assert not (task / ".codex").exists()


def test_no_source_returns_empty_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No out-of-band source → copies nothing (turn then fails auth loudly)."""
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(tmp_path / "does-not-exist"))
    task = tmp_path / "task"
    task.mkdir()

    assert reinject_credentials("codex", task) == []
    assert codex_credential_source() is None
