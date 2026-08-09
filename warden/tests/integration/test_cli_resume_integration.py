"""Real-token proof that session resume fires through the WIRED ChatAPI path.

Unlike ``test_persistence_integration.py`` (which drives ``ClaudeCliSession``
directly), these tests exercise the exact path the CLI/relay drives:
``ChatAPI.send(..., session_id=<sid>)`` -> ``Orchestrator.send_message`` ->
the 3-way session lookup -> DB resume -> ``claude --resume``.

To make the resume genuinely fire we keep everything hermetic and shared:

- The SessionIndex/SessionDB default path is monkeypatched to a tmp DB BEFORE
  any ChatAPI is built, so a *fresh* ChatAPI instance (its own SessionManager)
  finds the earlier session's row and takes the DB-resume branch
  (orchestrator.py step 3). The DB row's ``provider`` must equal the current
  provider (``claude-cli``) for resume to fire.
- ``base_dir`` / ``state_root`` live under ``tmp_path`` so the persistence
  restore unit (task folder + pinned ``.claude-home``) is isolated per test.

Persistence pins ``CLAUDE_CONFIG_DIR`` into a fresh task-local ``.claude-home``,
which STRANDS the macOS Keychain login. The autouse auth fixture exports
``CLAUDE_CODE_OAUTH_TOKEN`` so the token flows os.environ -> home_env ->
subprocess (see docs/reference/provider-auth-and-home-isolation.md). Without a
token the module SKIPs — an env gap, never a fake pass.

The load-bearing assertions are DETERMINISTIC (session-id reuse via
``SessionCreatedEvent(resumed=True)``, no error events, skill isolation, distinct
task dirs + archive keys). Model-content continuity checks are SECONDARY and
phrased leniently, because LLM output varies turn to turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import warden.orchestrator.session.db as session_db
from warden.config.models import HarnessConfig
from warden.drive.api import ChatAPI
from warden.workspace.bootstrap import bootstrap
from warden.persistence.keys import archive_key, task_dir
from warden.schemas.events import (
    ErrorEvent,
    MessageEvent,
    SessionCreatedEvent,
)

pytestmark = [
    pytest.mark.integration,
    # claude-cli is RETIRED at the factory (D7 — provider='claude-cli' raises
    # NotImplementedError). This module drives that dead path, so it can never pass
    # without un-retiring it. Equivalent coverage now lives elsewhere:
    #   * hermetic 3-way / DB resume — tests/core/test_session_resume.py
    #   * real-SDK persistence-active resume + skill isolation — the
    #     `--claude-session` / `--claude-crash` Docker bed gates (docs/08, docs/09).
    # Kept (mv-only) as the record of the retired path; skipped, not deleted.
    pytest.mark.skip(reason="claude-cli retired (D7); see module docstring / replacement coverage"),
    pytest.mark.skipif(
        not shutil.which("claude"), reason="claude CLI not installed"
    ),
]

# Toy skill fixtures (plan §9.5).
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skills"
_COUNTING_SKILL = _FIXTURES / "counting"
_PHONETIC_SKILL = _FIXTURES / "phonetic"

_PROVIDER = "claude-cli"


# --- auth seeding (mirrors test_persistence_integration.py) -------------------


def _oauth_token_from_keychain() -> str | None:
    """Best-effort read of the Claude Code OAuth access token from the Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
        token = data["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


@pytest.fixture(autouse=True, scope="module")
def _ensure_cli_auth() -> None:
    """Ensure the subprocess env carries an auth token; SKIP the module if not.

    The pinned ``CLAUDE_CONFIG_DIR`` has no Keychain access, so a token in
    ``os.environ`` is the only way the subprocess authenticates. We never write
    the token to disk.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
        return
    token = _oauth_token_from_keychain()
    if not token:
        pytest.skip(
            "claude CLI is not authenticated: no CLAUDE_CODE_OAUTH_TOKEN / "
            "ANTHROPIC_API_KEY in env and no OAuth token in the Keychain."
        )
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token


# --- shared helpers -----------------------------------------------------------


def _hermetic_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point SessionDB's default path at a tmp DB so ChatAPI instances share it.

    Must run BEFORE any ChatAPI is constructed (SessionManager -> SessionIndex ->
    SessionDB captures ``_DEFAULT_DB_PATH`` at construction time).
    """
    monkeypatch.setattr(session_db, "_DEFAULT_DB_PATH", tmp_path / "sessions.db")


def _make_api(tmp_path: Path, user: str, task: str) -> ChatAPI:
    """Build a persistence-active claude-cli ChatAPI rooted under ``tmp_path``.

    ``base_dir``/``state_root`` are shared across instances so restore units and
    archive keys resolve consistently; each instance carries its own
    user_id/task_id.
    """
    base_dir = tmp_path / "workspaces"
    config = HarnessConfig()
    config.provider.provider = _PROVIDER
    config.workspace.user_id = user
    config.workspace.task_id = task
    config.workspace.base_dir = str(base_dir)
    config.persistence.state_root = str(tmp_path / "store")
    return ChatAPI(config, repo_path=str(task_dir(base_dir, user, task)))


def _bootstrap_task(tmp_path: Path, user: str, task: str, skill: Path) -> Path:
    """mkdir the task folder + bootstrap ``skill`` into it before the first turn.

    ``ensure_restored`` is a no-op for a fresh unbacked dir, so the skill must be
    planted on disk here for the model turn to see it. Returns the task dir.
    """
    td = task_dir(tmp_path / "workspaces", user, task)
    td.mkdir(parents=True, exist_ok=True)
    bootstrap(td, skills=[skill], agents=[])
    assert (td / ".claude" / "skills" / skill.name).is_dir()
    return td


async def _run(api: ChatAPI, prompt: str, *, session_id: str | None = None):
    """Drive ``api.send`` and return (reply_text, captured_sid, resumed).

    Aggregates ``text`` MessageEvents and prefers the final ``result`` string.
    Captures the session id from ``SessionCreatedEvent`` (and whether it flagged
    ``resumed``). An ``ErrorEvent`` fails the test (LAW 4: never swallowed).
    """
    parts: list[str] = []
    result_text: str | None = None
    captured_sid: str | None = None
    resumed = False

    async for event in api.send(prompt, session_id=session_id):
        if isinstance(event, SessionCreatedEvent):
            captured_sid = event.session_id
            resumed = resumed or event.resumed
        elif isinstance(event, ErrorEvent):
            pytest.fail(f"claude CLI error event: {event.text!r}")
        elif isinstance(event, MessageEvent):
            content = event.content or {}
            if event.kind == "text":
                text = content.get("text")
                if text:
                    parts.append(text)
            elif content.get("subtype") == "result":
                res = content.get("result")
                if isinstance(res, str) and res:
                    result_text = res

    reply = result_text if result_text else "".join(parts)
    return reply, captured_sid, resumed


# --- Test 1: resume continues the conversation via the wired path -------------


@pytest.mark.timeout(240)
def test_cli_resume_continues_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plants a count; a fresh B resumes the same sid via DB and continues."""

    async def _body() -> None:
        _hermetic_db(monkeypatch, tmp_path)
        user, task = "default", "task_1"
        _bootstrap_task(tmp_path, user, task, _COUNTING_SKILL)

        # Turn A: fresh session plants a count, captures its sid.
        api_a = _make_api(tmp_path, user, task)
        await api_a.init()
        try:
            reply_a, sid, resumed_a = await _run(
                api_a,
                "Let's count. Count from 1 to 10 and remember the last number.",
            )
        finally:
            await api_a.close()

        assert sid, "turn A must yield a session id via SessionCreatedEvent"
        assert not resumed_a, "turn A must be a fresh (not resumed) session"
        assert "10" in reply_a, f"turn A reply should mention 10: {reply_a!r}"

        # Turn B: brand-new ChatAPI (own SessionManager) resumes via the tmp DB.
        api_b = _make_api(tmp_path, user, task)
        await api_b.init()
        try:
            reply_b, sid_b, resumed_b = await _run(
                api_b,
                "What number did we stop at? Continue with the next five numbers.",
                session_id=sid,
            )
        finally:
            await api_b.close()

        # DETERMINISTIC: resume genuinely fired through the wired path — the DB
        # row was found and reused (same sid, resumed flag set).
        assert resumed_b, "turn B must resume via the DB path (resumed=True)"
        assert sid_b == sid, "turn B must reuse turn A's session id"
        assert reply_b.strip(), "resumed reply must be non-empty"

        # SECONDARY (lenient): continuity with the earlier count.
        low = reply_b.lower()
        assert any(tok in low for tok in ("11", "15", "10")), (
            f"resumed reply should show counting continuity: {reply_b!r}"
        )

    asyncio.run(_body())


# --- Test 2: two-skill isolation via the wired API ----------------------------


@pytest.mark.timeout(300)
def test_two_skill_isolation_via_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two tenants x two skills stay isolated; each resumes its own memory."""

    async def _body() -> None:
        _hermetic_db(monkeypatch, tmp_path)
        u1, t1 = "default", "task_1"  # counting, remembers 42
        u2, t2 = "userb", "task_2"    # phonetic, remembers zebra

        td1 = _bootstrap_task(tmp_path, u1, t1, _COUNTING_SKILL)
        td2 = _bootstrap_task(tmp_path, u2, t2, _PHONETIC_SKILL)

        # Plant a distinct memory in each tenant via its own ChatAPI.
        api1 = _make_api(tmp_path, u1, t1)
        await api1.init()
        try:
            _, sid1, r1_new = await _run(
                api1,
                "Please remember the number 42. Just confirm you'll remember it.",
            )
        finally:
            await api1.close()

        api2 = _make_api(tmp_path, u2, t2)
        await api2.init()
        try:
            _, sid2, r2_new = await _run(
                api2,
                "Please remember the word zebra. Just confirm you'll remember it.",
            )
        finally:
            await api2.close()

        assert sid1 and sid2, "both planting turns must yield session ids"
        assert not r1_new and not r2_new, "planting turns must be fresh sessions"
        assert sid1 != sid2, "the two tenants must get distinct session ids"

        # DETERMINISTIC: per-task skill isolation — each task carries ONLY its own.
        assert (td1 / ".claude" / "skills" / "counting").is_dir()
        assert not (td1 / ".claude" / "skills" / "phonetic").exists()
        assert (td2 / ".claude" / "skills" / "phonetic").is_dir()
        assert not (td2 / ".claude" / "skills" / "counting").exists()

        # DETERMINISTIC: distinct task dirs AND distinct archive keys.
        assert td1 != td2
        base_dir = tmp_path / "workspaces"
        assert task_dir(base_dir, u1, t1) == td1
        assert task_dir(base_dir, u2, t2) == td2
        cfg1 = api1._orchestrator._persist_cfg  # noqa: SLF001 (test introspection)
        cfg2 = api2._orchestrator._persist_cfg  # noqa: SLF001
        key1 = archive_key(cfg1, u1, t1)
        key2 = archive_key(cfg2, u2, t2)
        assert key1 == "v1/default/task_1.tar.gz"
        assert key2 == "v1/userb/task_2.tar.gz"
        assert key1 != key2

        # Resume each tenant (fresh ChatAPI) and ask it to recall its own memory.
        api1b = _make_api(tmp_path, u1, t1)
        await api1b.init()
        try:
            recall1, sid1b, resumed1 = await _run(
                api1b,
                "What number did I ask you to remember?",
                session_id=sid1,
            )
        finally:
            await api1b.close()

        api2b = _make_api(tmp_path, u2, t2)
        await api2b.init()
        try:
            recall2, sid2b, resumed2 = await _run(
                api2b,
                "What word did I ask you to remember?",
                session_id=sid2,
            )
        finally:
            await api2b.close()

        # DETERMINISTIC: each resumed via its own DB row (no collision/cross-talk).
        assert resumed1 and resumed2, "both recalls must resume via the DB path"
        assert sid1b == sid1
        assert sid2b == sid2
        assert recall1.strip() and recall2.strip()

        # SECONDARY (lenient): each leans toward its OWN memory. Isolation proof
        # is the filesystem/key checks above; content is best-effort.
        assert "42" in recall1, f"tenant 1 should recall 42: {recall1!r}"
        assert "zebra" in recall2.lower(), (
            f"tenant 2 should recall zebra: {recall2!r}"
        )

    asyncio.run(_body())
