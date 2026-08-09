"""M1 — Session + Permission hardening (SESS-1, SESS-2, PERM-2).

Hermetic unit tests for the workflow init-binding + per-turn tool_scope
contract. No LLM / subprocess / network — a fake SessionManager drives every
path (mirrors ``test_orchestrator_errors.py``). Async style: ``asyncio.run``
in sync tests.

Covers:
- SESS-1(a): the permission surface is fixed at init — a same-workflow send
  does NOT re-derive the checker (no hot-swap).
- SESS-1(b): a send naming a DIFFERENT workflow raises WorkflowMismatchError;
  the bound session is untouched.
- SESS-2 (3b): resume rebuilds the checker + deny-baseline from the STORED
  workflow column, not transient memory.
- PERM-2 (3c): per-turn tool_scope narrows this turn's surface; the next send
  without it restores; the dead construction-fixed branch is gone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultDeny

import warden.orchestrator.session.manager as manager_mod
from warden.orchestrator.orchestrator import (
    Orchestrator,
    WorkflowMismatchError,
)
from warden.orchestrator.session.db import SessionDB
from warden.orchestrator.session.index import SessionIndex
from warden.orchestrator.session.manager import SessionManager
from warden.orchestrator.stream_runtime import resolve_turn_session
from warden.schemas.tool_scope import ToolScope

from warden.tests.core.test_orchestrator_errors import (
    _FakeSession,
    _FakeSessionManager,
    _drain,
)
from warden.tests.core.test_session_resume import ClaudeSession


def _write_deny_bash_workflow(repo: Path, name: str) -> None:
    """Write ``<repo>/.workflows/<name>.yaml`` that HARD-denies Bash."""
    wf_dir = repo / ".workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / f"{name}.yaml").write_text(
        "name: {n}\n"
        "description: deny bash\n"
        "permissions:\n"
        "  mode: read_only\n"
        "  tool_access:\n"
        "    deny: [Bash]\n".format(n=name)
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_orch(
    sm: _FakeSessionManager,
    *,
    workflow: str | None = None,
    repo_path: Path | None = None,
) -> Orchestrator:
    return Orchestrator(
        session_manager=sm,
        repo_path=repo_path or Path("."),
        workflow=workflow,
    )


# === SESS-1(a): surface fixed at init — no hot-swap re-derivation ===========

def test_no_current_workflow_name_attribute() -> None:
    """The hot-swap tracking field is gone — the surface is init-bound, not
    re-derived per send."""
    orch = _make_orch(_FakeSessionManager())
    assert not hasattr(orch, "_current_workflow_name")


def test_checker_identical_before_and_after_same_workflow_send() -> None:
    """A same-workflow send does NOT rebuild the PermissionChecker or the
    deny-baseline (SESS-1(a): policy fixed at init)."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        orch = _make_orch(sm, workflow=None)

        checker_before = orch._permission_checker
        baseline_before = orch._deny_baseline

        # Re-stating the bound workflow (None here) must not re-derive anything.
        await _drain(orch, "hi", workflow=None)

        assert orch._permission_checker is checker_before
        assert orch._deny_baseline is baseline_before

    _run(_test())


# === SESS-1(b): different-workflow send rejected ============================

def test_different_workflow_send_raises_and_leaves_session_untouched() -> None:
    """A send naming a workflow different from the init-bound one raises
    WorkflowMismatchError; the checker is untouched and no session is created."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        orch = _make_orch(sm, workflow="alpha")
        checker_before = orch._permission_checker

        raised = False
        try:
            await _drain(orch, "hi", workflow="beta")
        except WorkflowMismatchError:
            raised = True

        assert raised, "expected WorkflowMismatchError on a different-workflow send"
        assert orch._permission_checker is checker_before
        assert not sm.create_calls, "mismatched send must not create a session"

    _run(_test())


def test_same_workflow_send_allowed() -> None:
    """Re-stating the bound workflow by name is allowed (an assertion, not a
    re-point)."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        orch = _make_orch(sm, workflow="alpha")
        # Should not raise; a session gets created.
        await _drain(orch, "hi", workflow="alpha")
        assert sm.create_calls

    _run(_test())


def test_new_workflow_uses_a_new_session_with_its_own_id() -> None:
    """SESS-1: switching workflow is a NEW-session act — a fresh orchestrator
    bound to the new workflow mints its own session_id, distinct from the old
    session, never re-pointing the original."""

    async def _test() -> None:
        sm_alpha = _FakeSessionManager(create_session=_FakeSession(session_id="s-alpha"))
        orch_alpha = _make_orch(sm_alpha, workflow="alpha")
        await _drain(orch_alpha, "hi", workflow="alpha")
        assert orch_alpha._current_session_id == "s-alpha"

        sm_beta = _FakeSessionManager(create_session=_FakeSession(session_id="s-beta"))
        orch_beta = _make_orch(sm_beta, workflow="beta")
        await _drain(orch_beta, "hi", workflow="beta")

        assert orch_beta._workflow_name == "beta"
        assert orch_beta._current_session_id == "s-beta"
        assert orch_beta._current_session_id != orch_alpha._current_session_id

    _run(_test())


# === SESS-2 (3b): persist + restore the workflow ===========================

def _file_manager(tmp_path: Path) -> SessionManager:
    """SessionManager backed by a real file SQLite DB under tmp_path."""
    return SessionManager(index=SessionIndex(SessionDB(tmp_path / "sessions.db")))


def test_db_persists_and_reads_back_workflow_column(tmp_path: Path) -> None:
    """The sessions table has a ``workflow`` column: register writes it, get
    reads it back (N9 — the column must exist so resume can rebuild policy)."""

    async def _test() -> None:
        db = SessionDB(tmp_path / "sessions.db")
        await db.init()
        try:
            await db.register(
                session_id="s-wf",
                provider="claude",
                workspace_path=str(tmp_path),
                workflow="study",
            )
            row = await db.get("s-wf")
            assert row is not None
            assert row["workflow"] == "study"
        finally:
            await db.close()

    _run(_test())


def test_resume_rebuilds_checker_from_stored_workflow(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    """SESS-2: a fresh orchestrator (bound to NO workflow) resuming a session
    stored under a deny-Bash workflow rebuilds the checker FROM THE DB — the
    resumed session still hard-denies Bash, not from transient memory."""

    # Fake provider so resume() never spawns a real CLI.
    def _fake_create(provider: str = "claude", **kwargs: Any) -> ClaudeSession:
        return ClaudeSession(**kwargs)

    monkeypatch.setattr(manager_mod, "create_session", _fake_create)
    _write_deny_bash_workflow(tmp_path, "study")

    async def _test() -> None:
        mgr = _file_manager(tmp_path)
        await mgr.init()
        try:
            # Seed a DB row created UNDER workflow "study".
            await mgr._index.register(
                "s-study", "study", provider="claude",
                workspace_path=str(tmp_path),
            )

            # Fresh orchestrator bound to NO workflow — its checker is permissive.
            orch = _make_orch(mgr, workflow=None, repo_path=tmp_path)
            before = orch._permission_checker.evaluate("Bash", {"command": "ls"})
            assert before.source == "mode"  # permissive default, not a hard deny

            await orch.resume_session("s-study")

            # The surface was rebuilt from the STORED workflow, not memory.
            assert orch._workflow_name == "study"
            after = orch._permission_checker.evaluate("Bash", {"command": "ls"})
            assert after.allowed is False
            assert after.source == "tool_access"  # hard deny from "study"
        finally:
            await mgr.close_all()
            await mgr.close_index()

    _run(_test())


# === Provider match hardened off the class-name string (N9) =================

def test_step1_reuse_matches_on_stable_provider_key_not_class_name() -> None:
    """resolve_turn_session Step-1 reuses a live session by a STABLE provider
    key (``PROVIDER``), not a fragile class-name string — a session whose class
    is named arbitrarily is still reused when its PROVIDER matches."""

    class WeirdlyNamedClaudeThing(_FakeSession):
        PROVIDER = "claude"

    async def _test() -> None:
        live = WeirdlyNamedClaudeThing(session_id="s-live")
        sm = _FakeSessionManager(active={"s-live": live})

        session, is_resumed, cur, resumed_event = await resolve_turn_session(
            session_manager=sm,
            session_id="s-live",
            current_session_id=None,
            provider="claude",
            model=None,
            can_use_tool=lambda *_a, **_k: None,
            disallowed_tools=[],
            system_prompt=None,
            custom_tools=None,
            repo_path=Path("."),
            provider_kwargs=None,
        )

        assert session is live, "matching PROVIDER key must reuse the live session"
        assert is_resumed is False
        assert cur == "s-live"
        assert resumed_event is None

    _run(_test())


# === PERM-2 (3c): per-turn tool_scope is a ToolScope-stage input ===========

def test_per_turn_tool_scope_blocks_tool_for_that_turn() -> None:
    """send(tool_scope=deny{Bash}) narrows THIS turn: the per-turn arg becomes
    the active scope (the old construction-fixed dead branch is now wired) and
    _can_use_tool denies Bash by scope."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        orch = _make_orch(sm)  # ctor tool_scope defaults to None
        await _drain(orch, "hi", tool_scope=ToolScope(denied=["Bash"]))

        assert orch._active_tool_scope == ToolScope(denied=["Bash"])
        result = await orch._can_use_tool("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "tool scope" in result.message

    _run(_test())


def test_per_turn_scope_restores_on_next_send_without_it() -> None:
    """The next send WITHOUT a tool_scope restores the construction default —
    the tool blocked last turn is no longer scope-blocked."""

    async def _test() -> None:
        sm = _FakeSessionManager(create_session=_FakeSession(session_id="s-ok"))
        orch = _make_orch(sm)  # ctor default None → nothing scope-denied
        await _drain(orch, "one", tool_scope=ToolScope(denied=["Bash"]))
        assert orch._active_tool_scope == ToolScope(denied=["Bash"])

        await _drain(orch, "two")  # no per-turn scope → restore ctor default
        assert orch._active_tool_scope is None
        result = await orch._can_use_tool("Bash", {"command": "ls"}, None)
        assert not (
            isinstance(result, PermissionResultDeny) and "tool scope" in result.message
        )

    _run(_test())
