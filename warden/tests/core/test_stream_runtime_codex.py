"""Hermetic proof of bug 4b — Codex through the orchestrator emits events.

``CodexSdkSession.send`` yields already-normalized ChatMessage dicts keyed
``"kind"`` (via ``notification_to_event``). The orchestrator's per-turn handler
must pass those through. Before the fix, ``get_message_handler("codex")``
returned the legacy ``transform_codex_message`` — which branches on the absent
``event["type"]`` and therefore returned ``[]`` for *every* SDK event, so the
whole codex path produced no text/tool output. These tests pin the fix:

  * the legacy exec handler still drops a "kind"-keyed dict (fail-before shape);
  * the SDK passthrough keeps it (pass-after);
  * end-to-end, a fake ``CodexSdkSession`` driven through the real Orchestrator
    yields exactly one ``MessageEvent(kind="text")`` (was zero).

No live services, no credentials.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import warden.orchestrator.session.manager as manager_mod
from warden.orchestrator.orchestrator import Orchestrator
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.orchestrator.stream_runtime import get_message_handler
from warden.providers.codex.message_handler import transform_codex_message
from warden.schemas.events import MessageEvent


def _sdk_text_msg(sid: str) -> dict[str, Any]:
    """A message shaped exactly like ``CodexSdkSession.send`` yields (kind-keyed)."""
    return {
        "kind": "text",
        "id": "evt-1",
        "sessionId": sid,
        "timestamp": 0.0,
        "text": "hi",
    }


def test_codex_handler_is_passthrough_not_the_stale_type_handler() -> None:
    """The codex handler keeps a "kind"-keyed dict; the legacy handler drops it."""
    sid = "codex-thread-x"
    msg = _sdk_text_msg(sid)

    # fail-before shape: the legacy exec handler branches on the absent "type"
    # key, so it returns [] for the SDK's "kind"-keyed dict — the bug 4b drop.
    assert transform_codex_message(dict(msg), sid) == []

    # pass-after: the wired handler is the passthrough; one event survives.
    handler = get_message_handler("codex")
    out = handler(dict(msg), sid)
    assert out == [msg]


def test_codex_handler_guards_non_dict() -> None:
    """A non-dict (schema drift) degrades to a dropped event, never a crash."""
    handler = get_message_handler("codex")
    assert handler(object(), "sid") == []  # type: ignore[arg-type]
    assert handler(None, "sid") == []  # type: ignore[arg-type]


def test_codex_handler_output_becomes_a_text_message_event() -> None:
    """Reproduce the orchestrator's transform: handler output → MessageEvent."""
    sid = "codex-thread-y"
    handler = get_message_handler("codex")

    events: list[MessageEvent] = []
    for ws_msg in handler(_sdk_text_msg(sid), sid):
        kind = ws_msg.pop("kind", "text")
        ws_msg.pop("id", None)
        ws_msg.pop("timestamp", None)
        msg_sid = ws_msg.pop("sessionId", sid)
        events.append(MessageEvent(kind=kind, content=ws_msg, session_id=msg_sid))

    assert len(events) == 1
    assert events[0].kind == "text"
    assert events[0].content["text"] == "hi"
    assert events[0].session_id == sid


# ---------------------------------------------------------------------------
# End-to-end: a fake CodexSdkSession through the REAL Orchestrator
# ---------------------------------------------------------------------------


class CodexSdkSession:
    """Fake codex SDK session: captures a thread id, yields one "kind" dict.

    Named after the REAL class so the resolver's type-guard behaves honestly.
    """

    def __init__(self, *, resume_session_id: str | None = None, **_: Any) -> None:
        self.session_id: str | None = resume_session_id
        self.jsonl_path: str | None = None
        self._started = False

    async def start(self) -> None:
        self._started = True
        if self.session_id is None:
            self.session_id = "codex-thread-1"

    async def send(self, prompt: str):
        yield _sdk_text_msg(self.session_id or "")

    async def stop(self) -> None:  # pragma: no cover - only on cancel
        pass

    async def close(self) -> None:
        self._started = False


def test_codex_emits_message_event_through_orchestrator(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The FULL path: a codex turn yields ≥1 MessageEvent (was 0 before 4b)."""

    def _fake_create(provider: str = "claude", **kwargs: Any) -> CodexSdkSession:
        return CodexSdkSession(**kwargs)

    monkeypatch.setattr(manager_mod, "create_session", _fake_create)

    async def _body() -> None:
        mgr = SessionManager(index=SessionIndex(SessionDB(tmp_path / "sessions.db")))
        await mgr.init()
        orch = Orchestrator(session_manager=mgr, repo_path=tmp_path)
        try:
            events = [
                ev async for ev in orch.send_message("hello", provider="codex")
            ]
        finally:
            await orch.close()
            await mgr.close_all()
            await mgr.close_index()

        text_events = [
            e for e in events if isinstance(e, MessageEvent) and e.kind == "text"
        ]
        assert len(text_events) >= 1, "codex must emit ≥1 text MessageEvent (bug 4b)"
        assert any(e.content.get("text") == "hi" for e in text_events)

    asyncio.run(_body())
