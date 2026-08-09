"""Integration proof for v16 Phase 2 persistence (Steps 3 & 5).

These tests exercise the REAL ``claude`` CLI end-to-end against the merged
persistence core:

- ``test_resume_recalls_count`` (Step 3): bootstrap -> count -> snapshot ->
  DELETE -> ensure_restored -> resume. Proves a task survives
  delete->restore->resume with its session and workspace intact.
- ``test_two_tenants_isolated`` (Step 5): two users x two tasks x two skills.
  Proves per-task skill isolation and archive-key uniqueness on restore, with
  no cross-contamination.

The load-bearing assertions are DETERMINISTIC (filesystem round-trip, skill
isolation, key uniqueness, session-id reuse). Model-content checks are SECONDARY
and phrased leniently, because LLM output varies turn to turn.

Prompts are pure text (no tool use) so ``claude -p`` never hits a permission
prompt. The tests SKIP (not fail) when the ``claude`` CLI is absent; when the
CLI is present but unauthenticated they surface the CLI's own error event.

Design contract: plan §5 Step 3 / Step 5, §9.5 (toy skills), §9.7 (test map).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from warden.persistence import (
    LocalFileBackend,
    PersistenceConfig,
)
from warden.workspace import ensure_restored, snapshot
from warden.workspace.bootstrap import bootstrap
from warden.persistence.keys import archive_key
from warden.providers.claude.cli_session import ClaudeCliSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not shutil.which("claude"), reason="claude CLI not installed"
    ),
]

# Toy skill fixtures created in Step 2 (plan §9.5).
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skills"
_COUNTING_SKILL = _FIXTURES / "counting"
_PHONETIC_SKILL = _FIXTURES / "phonetic"


# --- auth seeding -------------------------------------------------------------
#
# These tests pin each session's home into the task folder via
# ``CLAUDE_CONFIG_DIR`` (plan §4.4 — one self-contained restore unit). But a
# fresh task-local config dir has NO credentials, and on macOS the real ones live
# in the Keychain (keyed to the default home), so file-based cred isolation
# strands auth — exactly the failure §4.4 guardrail 1 warns about.
#
# The plan's remedy is an env token (``CLAUDE_CODE_OAUTH_TOKEN``), which
# ``ClaudeCliSession.send()`` inherits from ``os.environ`` alongside the pinned
# ``CLAUDE_CONFIG_DIR``. If that token is not already exported, we source it once
# from the local Keychain (mirroring what production's ``home_env`` injects). If
# neither path yields a token, we SKIP rather than fail — an unauthenticated CLI
# is an environment gap, not a defect in the persistence flow under test.


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

    ``ClaudeCliSession.send()`` passes ``{**os.environ, CLAUDE_CONFIG_DIR: ...}``
    to the subprocess, so a token present in ``os.environ`` is inherited. We do
    NOT mutate any file and NEVER write the token to disk.
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


def _make_cfg(tmp_path: Path) -> tuple[PersistenceConfig, LocalFileBackend]:
    """Build a hermetic config + local backend rooted under ``tmp_path``."""
    cfg = PersistenceConfig(
        base_dir=tmp_path / "workspaces",
        state_root=tmp_path / "store",
    )
    backend = LocalFileBackend(cfg.state_root, cfg.exclude_patterns)
    return cfg, backend


async def _run(session: ClaudeCliSession, prompt: str) -> str:
    """Consume ``session.send(prompt)`` and return the assistant's reply text.

    Aggregates ``assistant`` text blocks and prefers the final ``result`` string
    when present. An ``error`` event fails the test (LAW 4: never swallowed).
    """
    parts: list[str] = []
    result_text: str | None = None

    async for event in session.send(prompt):
        etype = event.get("type")
        if etype == "assistant":
            content = event.get("message", {}).get("content", []) or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if text:
                        parts.append(text)
        elif etype == "result":
            res = event.get("result")
            if isinstance(res, str) and res:
                result_text = res
        elif etype == "error":
            pytest.fail(f"claude CLI error event: {event.get('message')!r}")

    if result_text:
        return result_text
    return "".join(parts)


async def _plant_memory(
    td: Path, config_dir: Path, session_id_hint: str, prompt: str
) -> tuple[str, str]:
    """Start session A in ``td``, send ``prompt``, return (reply, session_id).

    ``session_id_hint`` pins a deterministic new-session id so distinct tenants
    never collide. cwd is kept at ``td`` (resume is cwd-sensitive).
    """
    session = ClaudeCliSession(
        repo_path=td,
        claude_config_dir=config_dir,
        session_id=session_id_hint,
    )
    await session.start()
    try:
        reply = await _run(session, prompt)
        sid = session.session_id
        assert sid is not None, "session A must expose a session_id after a turn"
    finally:
        await session.close()
    return reply, sid


async def _resume_and_ask(
    td: Path, config_dir: Path, sid: str, prompt: str
) -> tuple[str, str]:
    """Resume session ``sid`` in ``td`` (same cwd + config_dir), ask ``prompt``.

    Returns (reply, session_id_after). The caller asserts the id was reused.
    """
    session = ClaudeCliSession(
        repo_path=td,
        claude_config_dir=config_dir,
        resume_session_id=sid,
    )
    await session.start()
    try:
        reply = await _run(session, prompt)
        return reply, session.session_id
    finally:
        await session.close()


# --- Test 1: resume (Step 3) --------------------------------------------------


@pytest.mark.timeout(240)
def test_resume_recalls_count(tmp_path: Path) -> None:
    """bootstrap -> count -> snapshot -> DELETE -> restore -> resume recalls count."""

    async def _body() -> None:
        cfg, backend = _make_cfg(tmp_path)
        user, task = "default", "task_1"
        config_subdir = ".claude-home"

        # 1-2. Guarded restore with nothing in the store: returns path, uncreated.
        td = await ensure_restored(cfg, backend, user, task)
        assert not td.exists()
        td.mkdir(parents=True)

        # 3. Scaffold the counting skill into the task folder.
        bootstrap(td, skills=[_COUNTING_SKILL], agents=[])
        assert (td / ".claude" / "skills" / "counting").is_dir()

        # 4. Deterministic workspace file we own (survives the round-trip).
        (td / "COUNT.txt").write_text("10")

        # 5. Session A: plant a count, capture the session id.
        config_dir = td / config_subdir
        reply, sid = await _plant_memory(
            td,
            config_dir,
            session_id_hint="11111111-1111-4111-8111-111111111111",
            prompt=(
                "Let's play a counting game. Count out loud from 1 to 10, then "
                "tell me the last number you reached. Remember it for later."
            ),
        )
        assert "10" in reply, f"session A reply should mention 10: {reply!r}"

        # 6. Snapshot to the store; the archive key must now exist.
        stats = await snapshot(cfg, backend, user, task)
        key = archive_key(cfg, user, task)
        assert stats["key"] == key
        assert await backend.exists(key)

        # 7. Delete the original task folder entirely.
        shutil.rmtree(td)
        assert not td.exists()

        # 8. Guarded restore rebuilds the exact same folder from the store.
        td2 = await ensure_restored(cfg, backend, user, task)
        assert td2 == td
        assert td.exists()
        # DETERMINISTIC: the workspace file round-trips byte-identically.
        assert (td / "COUNT.txt").read_text() == "10"
        # The session transcript dir travelled with the restore unit.
        assert (td / config_subdir).exists()

        # 9. Session B: resume the same id, continue counting.
        reply2, sid_after = await _resume_and_ask(
            td,
            td / config_subdir,
            sid,
            prompt=(
                "What number did we stop counting at? Continue by saying the "
                "next five numbers."
            ),
        )
        # DETERMINISTIC: session B reused the same session id.
        assert sid_after == sid, "session B must reuse session A's id"
        assert reply2.strip(), "resumed reply must be non-empty"
        # SECONDARY (lenient): continuity — any acceptable continuation token.
        low = reply2.lower()
        assert any(tok in low for tok in ("11", "12", "13", "14", "15", "10")), (
            f"resumed reply should show counting continuity: {reply2!r}"
        )

    asyncio.run(_body())


# --- Test 2: two-tenant capstone (Step 5) -------------------------------------


@pytest.mark.timeout(240)
def test_two_tenants_isolated(tmp_path: Path) -> None:
    """Two users x two tasks x two skills stay isolated across restore."""

    async def _body() -> None:
        cfg, backend = _make_cfg(tmp_path)
        config_subdir = ".claude-home"

        # Tenant 1: default/task_1, counting skill, secret number 42.
        u1, t1 = "default", "task_1"
        td1 = await ensure_restored(cfg, backend, u1, t1)
        td1.mkdir(parents=True)
        bootstrap(td1, skills=[_COUNTING_SKILL], agents=[])
        (td1 / "A.txt").write_text("alpha")
        reply1, sid1 = await _plant_memory(
            td1,
            td1 / config_subdir,
            session_id_hint="22222222-2222-4222-8222-222222222222",
            prompt=(
                "Please remember the secret number 42. Just confirm you will "
                "remember the secret number for later."
            ),
        )
        assert sid1 is not None
        await snapshot(cfg, backend, u1, t1)

        # Tenant 2: userb/task_2, phonetic skill, secret word zebra.
        u2, t2 = "userb", "task_2"
        td2 = await ensure_restored(cfg, backend, u2, t2)
        td2.mkdir(parents=True)
        bootstrap(td2, skills=[_PHONETIC_SKILL], agents=[])
        (td2 / "B.txt").write_text("bravo")
        reply2, sid2 = await _plant_memory(
            td2,
            td2 / config_subdir,
            session_id_hint="33333333-3333-4333-8333-333333333333",
            prompt=(
                "Please remember the secret word zebra. Just confirm you will "
                "remember the secret word for later."
            ),
        )
        assert sid2 is not None
        await snapshot(cfg, backend, u2, t2)

        # Keys are distinct per (user, task).
        key1 = archive_key(cfg, u1, t1)
        key2 = archive_key(cfg, u2, t2)
        assert key1 == "v1/default/task_1.tar.gz"
        assert key2 == "v1/userb/task_2.tar.gz"
        assert key1 != key2

        # Delete BOTH task dirs.
        shutil.rmtree(td1)
        shutil.rmtree(td2)
        assert not td1.exists()
        assert not td2.exists()

        # Restore BOTH from the store.
        r1 = await ensure_restored(cfg, backend, u1, t1)
        r2 = await ensure_restored(cfg, backend, u2, t2)
        assert r1 == td1 and td1.exists()
        assert r2 == td2 and td2.exists()

        # DETERMINISTIC: each task carries ONLY its own skill (no leak).
        skills1 = td1 / ".claude" / "skills"
        skills2 = td2 / ".claude" / "skills"
        assert (skills1 / "counting").is_dir()
        assert not (skills1 / "phonetic").exists()
        assert (skills2 / "phonetic").is_dir()
        assert not (skills2 / "counting").exists()

        # DETERMINISTIC: workspace files round-trip with correct contents.
        assert (td1 / "A.txt").read_text() == "alpha"
        assert (td2 / "B.txt").read_text() == "bravo"

        # Resume each session and ask it to recall its own secret.
        recall1, sid1_after = await _resume_and_ask(
            td1,
            td1 / config_subdir,
            sid1,
            prompt="What was the secret number I asked you to remember?",
        )
        recall2, sid2_after = await _resume_and_ask(
            td2,
            td2 / config_subdir,
            sid2,
            prompt="What was the secret word I asked you to remember?",
        )
        # DETERMINISTIC: each resumed session reused its own id (no collision).
        assert sid1_after == sid1
        assert sid2_after == sid2
        assert recall1.strip() and recall2.strip()

        # SECONDARY (lenient): each leans toward its OWN memory. Non-negotiable
        # isolation lives in the filesystem checks above; this is best-effort.
        assert "42" in recall1, f"tenant 1 should recall 42: {recall1!r}"
        assert "zebra" in recall2.lower(), (
            f"tenant 2 should recall zebra: {recall2!r}"
        )

    asyncio.run(_body())
