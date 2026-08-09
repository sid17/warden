"""Hermetic, CI-runnable proof of session resume / long-horizon continuity.

The integration suite (``tests/integration/test_cli_resume_integration.py``)
proves resume only with a LIVE ``claude`` CLI + OAuth token, so it SKIPs in CI.
This module reproduces the load-bearing, DETERMINISTIC assertions of that suite
with NO live services:

- No ``claude`` CLI / OAuth token — the provider is a fake session that simply
  echoes whatever ``resume_session_id`` it is handed.
- No DB server — the session index uses real *file-backed* SQLite under
  ``tmp_path`` (same approach as ``test_shared_session_index.py``), which is the
  DB seam the DB-resume branch reads.

We cover ``resolve_turn_session``'s 3-way lookup (client-active / orchestrator
in-memory / DB-backed resume) plus the fresh-create fallback, prove
``SessionManager.resume()`` pins/echoes the prior ``session_id`` (no new id),
and prove a resumed turn is flagged ``resumed=True`` via
``SessionCreatedEvent`` — the same continuity signal the live integration test
asserts, but hermetic.

Async is driven with ``asyncio.run(...)`` inside sync tests (this engine has no
pytest-asyncio). Each SQLite-backed lifecycle runs inside one coroutine so its
aiosqlite connection lives and dies within a single event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import warden.orchestrator.session.manager as manager_mod
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.orchestrator.stream_runtime import resolve_turn_session
from warden.schemas.events import SessionCreatedEvent


# ---------------------------------------------------------------------------
# Fake provider session
# ---------------------------------------------------------------------------
#
# ``resolve_turn_session`` step 1 gates an in-memory reuse on the session's
# stable ``PROVIDER`` key matching the turn's provider (N9). So the fake carries
# ``PROVIDER = "claude"`` for the in-memory-resume branch to fire.


class ClaudeSession:
    """Fake AgentProvider that echoes its resume id, mirroring the real SDK.

    Real providers capture ``session_id`` from the first streaming message; on
    resume, ``resume_session_id`` is passed through and reused verbatim (see
    ``providers.create_session``). This fake reproduces exactly that contract:
    on a fresh create it adopts a fixed synthetic id; on resume it echoes the
    id it was handed (no new id minted).
    """

    #: Stable provider key the resume path matches on (mirrors the real
    #: ``ClaudeSession.PROVIDER``; the resolver reads this, not the class name).
    PROVIDER = "claude"

    #: Fixed id a *fresh* fake session adopts once started (stands in for the
    #: id the real SDK would emit on its first message).
    FRESH_ID = "fake-fresh-sid"

    def __init__(self, *, resume_session_id: str | None = None, **_: Any) -> None:
        self._resume_session_id = resume_session_id
        # Fresh sessions have no id until start() (SDK captures it later); a
        # resume pins the id immediately from resume_session_id.
        self.session_id: str | None = resume_session_id
        self.jsonl_path: str | None = None
        self.started = False

    async def start(self) -> None:
        self.started = True
        if self.session_id is None:
            self.session_id = self.FRESH_ID

    async def close(self) -> None:
        self.started = False


def _install_fake_provider(monkeypatch: Any) -> None:
    """Patch ``create_session`` at the manager's import site to build fakes.

    ``manager.py`` does ``from ...providers import create_session``, so the name
    is bound in ``manager_mod`` — patch it there (not the source module).
    """

    def _fake_create(provider: str = "claude", **kwargs: Any) -> ClaudeSession:
        return ClaudeSession(**kwargs)

    monkeypatch.setattr(manager_mod, "create_session", _fake_create)


def _manager(tmp_path: Path) -> SessionManager:
    """SessionManager backed by a real file SQLite DB under tmp_path."""
    return SessionManager(index=SessionIndex(SessionDB(tmp_path / "sessions.db")))


# Common resolve_turn_session kwargs so each test states only what it varies.
def _resolve_kwargs(session_manager: SessionManager, repo_path: Path, **over: Any) -> dict:
    base: dict[str, Any] = dict(
        session_manager=session_manager,
        session_id=None,
        current_session_id=None,
        provider="claude",
        model=None,
        can_use_tool=None,
        disallowed_tools=[],
        system_prompt=None,
        custom_tools=None,
        repo_path=repo_path,
        provider_kwargs=None,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# SessionManager.resume() — pins/echoes the prior id, no new id minted
# ---------------------------------------------------------------------------


def test_resume_pins_prior_session_id(tmp_path: Path, monkeypatch: Any) -> None:
    """resume() reuses the original id verbatim and registers the session live."""
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "prior-session-42"
            # Seed a prior session row (as a first turn would have).
            await mgr._index.register(
                sid, "chat", provider="claude", workspace_path=str(tmp_path)
            )

            returned_sid, session = await mgr.resume(sid, repo_path=tmp_path)

            # No new id minted — the exact prior id flows through.
            assert returned_sid == sid
            assert session.session_id == sid
            # The fake proves the id came from resume_session_id, not FRESH_ID.
            assert session.session_id != ClaudeSession.FRESH_ID
            assert session.started is True
            # Now live in-memory under the same id.
            assert mgr.get(sid) is session
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resume_unknown_session_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """resume() on an id absent from the index raises (never fabricates)."""
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            raised = False
            try:
                await mgr.resume("nope-not-here", repo_path=tmp_path)
            except ValueError as exc:
                raised = "not found" in str(exc)
            assert raised, "resume of an unknown session must raise ValueError"
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# resolve_turn_session — the 3-way lookup + fresh-create fallback
# ---------------------------------------------------------------------------


def test_resolve_creates_fresh_when_no_id(tmp_path: Path, monkeypatch: Any) -> None:
    """Branch 4: no ids given -> a brand-new (non-resumed) session is created."""
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path)
            )
            assert session is not None
            assert is_resumed is False
            assert resumed_event is None
            assert current is None  # nothing pinned yet (id captured post-start)
            assert session.session_id == ClaudeSession.FRESH_ID
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resolve_reuses_client_active_session(tmp_path: Path, monkeypatch: Any) -> None:
    """Branch 1: client-supplied id maps to a live session of the right type."""
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            # Plant a live in-memory ClaudeSession under a known id.
            sid = "client-active-1"
            live = ClaudeSession(resume_session_id=sid)
            await live.start()
            mgr._sessions[sid] = live

            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid)
            )
            # Reused the SAME live object — no resume, no DB touch, no new create.
            assert session is live
            assert is_resumed is False
            assert resumed_event is None
            assert current == sid
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resolve_client_id_wrong_type_falls_through(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 1 guard: a live session of the WRONG provider type is not reused.

    The wrong-type fake is named ``ClaudeSession`` while the turn's provider is
    ``codex`` — a GENUINE mismatch regardless of the map value. (The original
    version named the fake ``CodexSession``, the *stale* map value, which would
    have spuriously *matched* a codex turn and masked bug 4a.) With no DB row to
    fall back on, this must land on fresh-create (branch 4).
    """
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "client-wrong-type"
            # A live ``ClaudeSession`` (name != codex's expected "CodexSdkSession")
            # planted under a codex turn — a genuine type mismatch.
            planted = ClaudeSession(resume_session_id=sid)
            await planted.start()
            mgr._sessions[sid] = planted

            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider="codex")
            )
            # Wrong type skipped; there is no DB row for sid, so a fresh session
            # is created (branch 4). current stays None (fresh id not captured).
            assert session is not planted, "wrong-type live session must NOT be reused"
            assert isinstance(session, ClaudeSession)  # fresh create via installer
            assert is_resumed is False
            assert resumed_event is None
            assert current is None
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resolve_reuses_orchestrator_current_session(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 2: no client id, but the orchestrator's current id is live."""
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            cur = "orch-current-7"
            live = ClaudeSession(resume_session_id=cur)
            await live.start()
            mgr._sessions[cur] = live

            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, current_session_id=cur)
            )
            assert session is live
            assert is_resumed is False
            assert resumed_event is None
            assert current == cur
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resolve_db_backed_resume_flags_resumed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 3: id absent in memory but present in the DB -> resume fires.

    This is the branch the live integration test proves only with a real CLI +
    token. Here a real file-SQLite row + a fake provider make it hermetic. The
    resumed turn must emit a ``SessionCreatedEvent(resumed=True)`` pinned to the
    ORIGINAL id — the exact continuity signal ``_run`` asserts in the
    integration suite.
    """
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "db-resume-99"
            # A prior turn's row exists in the DB, but NOT in this manager's
            # in-memory _sessions (simulating a fresh process/container).
            await mgr._index.register(
                sid, "chat", provider="claude", workspace_path=str(tmp_path)
            )
            assert mgr.get(sid) is None, "precondition: not in memory"

            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid)
            )

            # Resume genuinely fired via the DB path.
            assert is_resumed is True
            assert current == sid  # original id reused, no new id
            assert session.session_id == sid
            # The continuity event the caller must yield.
            assert isinstance(resumed_event, SessionCreatedEvent)
            assert resumed_event.resumed is True
            assert resumed_event.session_id == sid
            # And it is now live in memory under that same id.
            assert mgr.get(sid) is session
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


def test_resolve_db_resume_skipped_on_provider_mismatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Branch 3 guard: a DB row for a DIFFERENT provider does not resume.

    The row's provider (codex) != the turn's provider (claude), so the resume
    is skipped and a fresh claude session is created instead (branch 4).
    """
    _install_fake_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "db-provider-mismatch"
            await mgr._index.register(
                sid, "chat", provider="codex", workspace_path=str(tmp_path)
            )

            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider="claude")
            )
            assert is_resumed is False
            assert resumed_event is None
            assert isinstance(session, ClaudeSession)
            # Fresh session — did NOT adopt the prior id.
            assert session.session_id == ClaudeSession.FRESH_ID
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# End-to-end continuity: plant then resume via a FRESH manager (cross-process)
# ---------------------------------------------------------------------------


def test_cross_manager_db_resume_continuity(tmp_path: Path, monkeypatch: Any) -> None:
    """Manager A plants a session; a FRESH manager B resumes it via the shared DB.

    Mirrors the integration test's two-ChatAPI flow (turn A plants, a brand-new
    turn B resumes the same sid) but with fakes — proving long-horizon
    continuity across process boundaries with no live services.
    """
    _install_fake_provider(monkeypatch)
    db_path = tmp_path / "sessions.db"

    async def _plant() -> str:
        mgr = SessionManager(index=SessionIndex(SessionDB(db_path)))
        await mgr.init()
        try:
            # Turn A: fresh create, then register the captured id (as the
            # orchestrator does after the first streaming message).
            session = await mgr.create(repo_path=tmp_path, provider="claude")
            await mgr.register(
                session, provider="claude", workspace_path=str(tmp_path)
            )
            sid = session.session_id
            assert sid == ClaudeSession.FRESH_ID
            return sid
        finally:
            await mgr.close_all()
            await mgr.close_index()

    async def _resume(sid: str) -> None:
        # Manager B: brand-new instance, same DB file, empty in-memory table.
        mgr = SessionManager(index=SessionIndex(SessionDB(db_path)))
        await mgr.init()
        try:
            assert mgr.get(sid) is None, "fresh manager has nothing in memory"
            session, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid)
            )
            assert is_resumed is True
            assert current == sid
            assert session.session_id == sid
            assert isinstance(resumed_event, SessionCreatedEvent)
            assert resumed_event.resumed is True
            assert resumed_event.session_id == sid
        finally:
            await mgr.close_all()
            await mgr.close_index()

    sid = asyncio.run(_plant())
    asyncio.run(_resume(sid))


# ===========================================================================
# Multi-provider hermetic lifecycle suite — scenarios 2–7 (SCOPE §5, docs/08)
# ===========================================================================
#
# The resolver's Step-1 guard reuses a live in-memory session only when its
# STABLE provider key (``PROVIDER``) matches the turn's provider (N9 hardening:
# it no longer reads ``type(session).__name__``, so a class rename can't mask
# drift — the 4a trap is now structurally impossible). Each fake still carries
# both the REAL class *name* and the REAL ``PROVIDER`` key so the honest match
# is exercised:
#
#     claude → ClaudeSession   codex → CodexSdkSession   openharness → OpenHarnessSession


class CodexSdkSession(ClaudeSession):
    """Fake named after the REAL codex provider class (see bug 4a)."""

    PROVIDER = "codex"


class OpenHarnessSession(ClaudeSession):
    """Fake named after the REAL OpenHarness provider class."""

    PROVIDER = "openharness"


#: provider → the fake class ``create_session`` must build so the resolved
#: session's ``PROVIDER`` key matches the turn's provider honestly.
_FAKE_BY_PROVIDER: dict[str, type[ClaudeSession]] = {
    "claude": ClaudeSession,
    "codex": CodexSdkSession,
    "openharness": OpenHarnessSession,
}

_ALL_PROVIDERS = ["claude", "codex", "openharness"]


def _install_multi_provider(monkeypatch: Any) -> None:
    """Patch ``create_session`` to build the fake NAMED AFTER each real class.

    Unlike ``_install_fake_provider`` (always ``ClaudeSession``), this honors the
    ``provider`` argument so the resolved session's class name exercises the
    Step-1 type-guard for that provider honestly.
    """

    def _fake_create(provider: str = "claude", **kwargs: Any) -> ClaudeSession:
        return _FAKE_BY_PROVIDER[provider](**kwargs)

    monkeypatch.setattr(manager_mod, "create_session", _fake_create)


# --- Scenario 3 (S1/S2): create → capture id → register → DB row -----------


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_create_register_captures_id(
    provider: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """create() adopts the SDK-captured id; register() writes a durable row."""
    _install_multi_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            session = await mgr.create(repo_path=tmp_path, provider=provider)
            # id captured from the (fake) first streamed message on start().
            assert session.session_id == ClaudeSession.FRESH_ID
            # The resolved class is named after the REAL provider class.
            assert type(session).__name__ == _FAKE_BY_PROVIDER[provider].__name__
            # A discoverable transcript path becomes addressable once id known.
            session.jsonl_path = f"/transcripts/{session.session_id}.jsonl"

            await mgr.register(
                session, provider=provider, workspace_path=str(tmp_path)
            )
            row = await mgr._index.get(session.session_id)
            assert row is not None
            assert row["provider"] == provider
            assert row["workspace_path"] == str(tmp_path)
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# --- Scenario 4 (S3/S4-mech): resume after close, same process -------------


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_resume_after_close_reuses_id(
    provider: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """close() archives the row but the id remains resumable via the DB path."""
    _install_multi_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            session = await mgr.create(repo_path=tmp_path, provider=provider)
            await mgr.register(
                session, provider=provider, workspace_path=str(tmp_path)
            )
            sid = session.session_id
            await mgr.close(sid)
            assert mgr.get(sid) is None, "closed → out of the live map"

            s2, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider=provider)
            )
            assert is_resumed is True
            assert current == sid
            assert s2.session_id == sid
            assert isinstance(resumed_event, SessionCreatedEvent)
            assert resumed_event.resumed is True
            assert type(s2).__name__ == _FAKE_BY_PROVIDER[provider].__name__
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# --- Scenario 5 (S3): switch → come back reuses the LIVE session (no re-resume)


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_switch_and_come_back_reuses_live(
    provider: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """A live session addressed by id is REUSED, never re-resumed.

    A DB row also exists for the id, so a broken Step-1 guard would fall
    through to the DB-resume branch (Step 3) and wrongly re-resume. We spy on
    ``SessionManager.resume`` and assert it is never called. The ``codex`` case
    is the old bug-4a gate: matching on the stable ``PROVIDER`` key (N9) makes
    the stale-map failure mode structurally impossible — the live
    ``CodexSdkSession`` self-reports ``PROVIDER == "codex"`` and is reused.
    """
    _install_multi_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "live-reuse-1"
            live = _FAKE_BY_PROVIDER[provider](resume_session_id=sid)
            await live.start()
            mgr._sessions[sid] = live
            # A resumable DB row for the SAME id (the re-resume trap).
            await mgr._index.register(
                sid, "chat", provider=provider, workspace_path=str(tmp_path)
            )

            calls = {"n": 0}
            real_resume = mgr.resume

            async def _spy_resume(*a: Any, **k: Any):
                calls["n"] += 1
                return await real_resume(*a, **k)

            monkeypatch.setattr(mgr, "resume", _spy_resume)

            s2, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider=provider)
            )
            assert s2 is live, "must reuse the live in-memory session"
            assert is_resumed is False
            assert resumed_event is None
            assert current == sid
            assert calls["n"] == 0, "reuse must NOT trigger a re-resume"
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# --- Scenario 6 (S6): DB-resume in a FRESH manager (crash-recovery proxy) ---


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_db_resume_fresh_manager(
    provider: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """A brand-new manager (empty ``_sessions``) resumes via the shared SQLite."""
    _install_multi_provider(monkeypatch)
    db_path = tmp_path / "sessions.db"

    async def _plant() -> str:
        mgr = SessionManager(index=SessionIndex(SessionDB(db_path)))
        await mgr.init()
        try:
            session = await mgr.create(repo_path=tmp_path, provider=provider)
            await mgr.register(
                session, provider=provider, workspace_path=str(tmp_path)
            )
            return session.session_id
        finally:
            await mgr.close_all()
            await mgr.close_index()

    async def _resume(sid: str) -> None:
        mgr = SessionManager(index=SessionIndex(SessionDB(db_path)))
        await mgr.init()
        try:
            assert mgr.get(sid) is None, "fresh manager holds nothing in memory"
            s2, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider=provider)
            )
            assert is_resumed is True
            assert current == sid
            assert s2.session_id == sid
            assert isinstance(resumed_event, SessionCreatedEvent)
            assert resumed_event.resumed is True
        finally:
            await mgr.close_all()
            await mgr.close_index()

    sid = asyncio.run(_plant())
    asyncio.run(_resume(sid))


# --- Scenario 7 (S5): provider-mismatch on resume is rejected --------------


def test_provider_mismatch_resume_rejected(tmp_path: Path, monkeypatch: Any) -> None:
    """Resuming a CLAUDE row under the codex provider is rejected (create-fresh).

    The reverse of ``test_resolve_db_resume_skipped_on_provider_mismatch`` and,
    critically, it uses the multi-provider installer so the fresh session is a
    genuinely-named ``CodexSdkSession`` — proving no cross-attach occurs.
    """
    _install_multi_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            sid = "claude-row-cross"
            await mgr._index.register(
                sid, "chat", provider="claude", workspace_path=str(tmp_path)
            )
            s2, is_resumed, current, resumed_event = await resolve_turn_session(
                **_resolve_kwargs(mgr, tmp_path, session_id=sid, provider="codex")
            )
            assert is_resumed is False
            assert resumed_event is None
            assert type(s2).__name__ == "CodexSdkSession"
            # Fresh session — did NOT adopt the claude row's id.
            assert s2.session_id == ClaudeSession.FRESH_ID
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())


# --- Scenario 9 (S8): jsonl_path round-trips to the DB, non-null -----------


@pytest.mark.parametrize("provider", _ALL_PROVIDERS)
def test_jsonl_path_round_trips(
    provider: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """A session's ``jsonl_path`` persists to the index and reads back non-null."""
    _install_multi_provider(monkeypatch)

    async def _body() -> None:
        mgr = _manager(tmp_path)
        await mgr.init()
        try:
            session = await mgr.create(repo_path=tmp_path, provider=provider)
            expected = f"/transcripts/{provider}/{session.session_id}.jsonl"
            session.jsonl_path = expected
            await mgr.register(
                session, provider=provider, workspace_path=str(tmp_path)
            )
            row = await mgr._index.get(session.session_id)
            assert row is not None
            assert row["jsonl_path"] == expected
            assert row["jsonl_path"] is not None
        finally:
            await mgr.close_all()
            await mgr.close_index()

    asyncio.run(_body())
