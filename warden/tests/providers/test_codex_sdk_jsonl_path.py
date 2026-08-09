"""Hermetic proof of B-jsonl — CodexSdkSession populates ``jsonl_path``.

Parity with ``ClaudeSession``/``OpenHarnessSession`` (contract S8 — each
session's transcript is addressable via ``jsonl_path``). Codex writes its
rollout at ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl``;
``discover_jsonl_path`` matches it by the EXACT thread id under the pinned
``codex_home`` and takes no risky "most recent" fallback.

No live services — we plant a rollout file and call the pure discovery method.
"""

from __future__ import annotations

from pathlib import Path

from warden.providers.codex.sdk_session import CodexSdkSession


def _plant_rollout(home: Path, thread_id: str) -> Path:
    """Write a fake rollout under <home>/sessions/YYYY/MM/DD/ like codex does."""
    d = home / "sessions" / "2026" / "07" / "18"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-07-18T00-00-00-{thread_id}.jsonl"
    path.write_text('{"type":"session_meta"}\n')
    return path


def _session(tmp_path: Path, home: Path, thread_id: str) -> CodexSdkSession:
    sess = CodexSdkSession(repo_path=tmp_path, codex_home=home, session_id=thread_id)
    assert sess.session_id == thread_id  # pinned id stands in for the thread id
    return sess


def test_discover_finds_rollout_by_exact_thread_id(tmp_path: Path) -> None:
    home = tmp_path / "codexhome"
    tid = "019f7835-0a46-7633-ad0d-d145872d1d8b"
    planted = _plant_rollout(home, tid)

    sess = _session(tmp_path, home, tid)
    sess.discover_jsonl_path()

    assert sess.jsonl_path == str(planted)


def test_discover_no_match_leaves_none(tmp_path: Path) -> None:
    home = tmp_path / "codexhome"
    _plant_rollout(home, "some-other-thread")

    sess = _session(tmp_path, home, "our-thread-id")
    sess.discover_jsonl_path()

    # No file for OUR id → null pointer beats a wrong transcript.
    assert sess.jsonl_path is None


def test_discover_honors_pinned_home_not_global(tmp_path: Path) -> None:
    """A rollout in a DIFFERENT home must not be picked up (isolation, C4)."""
    other_home = tmp_path / "someone_elses_codex"
    tid = "shared-id"
    _plant_rollout(other_home, tid)

    pinned = tmp_path / "our_codex"  # empty — no sessions/ dir
    sess = _session(tmp_path, pinned, tid)
    sess.discover_jsonl_path()

    assert sess.jsonl_path is None


def test_discover_uses_ambient_codex_home_when_unpinned(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Un-pinned (persistence-off) turn: the subprocess writes to $CODEX_HOME,
    so discovery must look THERE — the real Docker-bed path (was the B-jsonl
    real-path miss). ``_codex_home`` is None but ``CODEX_HOME`` env is set."""
    import os

    home = tmp_path / "codexhome"
    tid = "ambient-thread"
    planted = _plant_rollout(home, tid)

    # No codex_home pinned; the subprocess-style CODEX_HOME env points at `home`.
    monkeypatch.setenv("CODEX_HOME", str(home))  # type: ignore[attr-defined]
    sess = CodexSdkSession(repo_path=tmp_path, session_id=tid)  # codex_home=None
    assert sess._codex_home is None
    sess.discover_jsonl_path()

    assert sess.jsonl_path == str(planted)
    assert os.environ.get("CODEX_HOME") == str(home)


def test_discover_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "codexhome"
    tid = "thread-x"
    _plant_rollout(home, tid)

    sess = _session(tmp_path, home, tid)
    sess.jsonl_path = "/already/set.jsonl"
    sess.discover_jsonl_path()

    assert sess.jsonl_path == "/already/set.jsonl"  # unchanged
