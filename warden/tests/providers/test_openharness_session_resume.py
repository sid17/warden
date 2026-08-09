"""Hermetic proof of bug B-OH — OpenHarness resume seeds history.

``OpenHarnessSession.start()`` builds a FRESH ``QueryEngine`` whose conversation
is empty, so before the fix a resumed session had NO memory of earlier turns —
``resume_session_id`` was stored but never consumed. Contract S4 (resume =
re-attach + MEMORY) therefore failed at the model level.

These tests pin the fix without any live Ollama:

  * ``_load_history`` parses the persisted transcript into ``ConversationMessage``
    objects and REPLACES the engine's history via ``load_messages``;
  * ``start()`` calls it ONLY on resume (a fresh session stays cold);
  * corrupt / missing transcripts fail soft (no crash).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import warden.providers.openharness.session as session_mod
from warden.providers.openharness.session import OpenHarnessSession


class _FakeEngine:
    """Stand-in for ``QueryEngine``: records ``load_messages`` / exposes messages."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self._messages: list = []

    def load_messages(self, messages: list) -> None:
        self._messages = list(messages)

    @property
    def messages(self) -> list:
        return list(self._messages)


def _write_transcript(root: Path, sid: str, entries: list[dict]) -> Path:
    """Write a transcript at ``<root>/sessions/<sid>.jsonl`` (the real layout)."""
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _resuming_session(tmp_path: Path, sid: str) -> OpenHarnessSession:
    """A session pinned to resume ``sid`` with its transcript root at tmp_path."""
    return OpenHarnessSession(
        repo_path=tmp_path,
        resume_session_id=sid,
        session_home=tmp_path,
    )


# --- _load_history: the load-bearing parse + seed ---------------------------


def test_load_history_seeds_engine_from_transcript(tmp_path: Path) -> None:
    """A planted user/assistant exchange is loaded into the engine on resume."""
    sid = "oh-resume-1"
    _write_transcript(
        tmp_path, sid,
        [
            {"type": "user", "sessionId": sid, "timestamp": 1.0,
             "text": "my favorite color is heliotrope"},
            {"type": "assistant", "sessionId": sid, "timestamp": 2.0,
             "text": "noted"},
        ],
    )
    sess = _resuming_session(tmp_path, sid)
    sess._engine = _FakeEngine()

    sess._load_history()

    msgs = sess._engine.messages
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content[0].text == "my favorite color is heliotrope"
    assert msgs[1].role == "assistant"
    assert msgs[1].content[0].text == "noted"


def test_load_history_no_transcript_is_cold_noop(tmp_path: Path) -> None:
    """No transcript on disk → the engine history stays empty (cold, no crash)."""
    sess = _resuming_session(tmp_path, "oh-missing")
    sess._engine = _FakeEngine()

    sess._load_history()

    assert sess._engine.messages == []


def test_load_history_skips_corrupt_lines(tmp_path: Path) -> None:
    """A corrupt JSONL line is skipped; valid turns still load (fail-soft)."""
    sid = "oh-corrupt"
    d = tmp_path / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{sid}.jsonl", "w") as f:
        f.write(json.dumps({"type": "user", "text": "hello"}) + "\n")
        f.write("{ this is not json\n")
        f.write(json.dumps({"type": "assistant", "text": "hi"}) + "\n")

    sess = _resuming_session(tmp_path, sid)
    sess._engine = _FakeEngine()

    sess._load_history()

    msgs = sess._engine.messages
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content[0].text for m in msgs] == ["hello", "hi"]


# --- start() wiring: seed on resume only ------------------------------------


async def _noop_health(self: Any) -> None:  # pragma: no cover - trivial
    return None


def test_start_seeds_history_only_on_resume(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """start() loads prior history when resuming, and stays cold when fresh."""
    import asyncio

    monkeypatch.setattr(session_mod, "QueryEngine", _FakeEngine)
    monkeypatch.setattr(
        OpenHarnessSession, "_check_ollama_health", _noop_health, raising=True
    )

    sid = "oh-start-resume"
    _write_transcript(
        tmp_path, sid,
        [{"type": "user", "sessionId": sid, "timestamp": 1.0,
          "text": "remember heliotrope"}],
    )

    # Resuming session → history seeded from the transcript.
    resumed = _resuming_session(tmp_path, sid)
    asyncio.run(resumed.start())
    assert [m.content[0].text for m in resumed._engine.messages] == [
        "remember heliotrope"
    ]

    # Fresh session (no resume id, no transcript) → cold engine.
    fresh = OpenHarnessSession(repo_path=tmp_path, session_home=tmp_path)
    asyncio.run(fresh.start())
    assert fresh._engine.messages == []
