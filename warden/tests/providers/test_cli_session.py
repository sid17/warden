"""Tests for providers.claude.cli_session — ClaudeCliSession lifecycle."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from warden.providers.claude.cli_session import ClaudeCliSession


# --- Fake subprocess for mocking ---

class FakeProcess:
    """Fake asyncio subprocess that yields controlled NDJSON lines."""

    def __init__(self, lines: list[str], returncode: int = 0):
        self._lines = lines
        self.returncode = returncode
        self.pid = 12345
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStderr()

    async def wait(self):
        return self.returncode


class FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = iter(lines)

    async def readline(self):
        try:
            line = next(self._lines)
            return (line + "\n").encode()
        except StopIteration:
            return b""


class FakeStderr:
    def __init__(self, data: bytes = b""):
        self._buf = data

    async def read(self, n: int = -1):
        # Mimic StreamReader.read(n): return up to n bytes, b"" at EOF.
        if n is None or n < 0:
            data, self._buf = self._buf, b""
            return data
        data, self._buf = self._buf[:n], self._buf[n:]
        return data


# --- Helpers ---

def _make_system_init(session_id: str = "test-session-uuid") -> str:
    return json.dumps({
        "type": "system", "subtype": "init",
        "session_id": session_id, "model": "claude-opus-4-6",
        "tools": ["Bash"],
    })


def _make_assistant_text(text: str = "Hello", session_id: str = "test-session-uuid") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "session_id": session_id,
    })


def _make_result(session_id: str = "test-session-uuid") -> str:
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "Hello", "session_id": session_id,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })


async def _collect_events(session, prompt="hello"):
    events = []
    async for event in session.send(prompt):
        events.append(event)
    return events


# --- Tests ---

def test_start_validates_cli_installed():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Claude CLI not found"):
                await session.start()
    asyncio.run(_run())


def test_start_succeeds_when_cli_installed():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            await session.start()
        assert session._started is True
    asyncio.run(_run())


def test_send_requires_start():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        with pytest.raises(RuntimeError, match="not started"):
            async for _ in session.send("hello"):
                pass
    asyncio.run(_run())


def test_send_first_turn_builds_correct_args():
    """First turn should NOT include --resume."""
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        captured_args = {}
        fake = FakeProcess([_make_system_init(), _make_assistant_text(), _make_result()])

        async def fake_exec(*args, **kwargs):
            captured_args["args"] = args
            captured_args["kwargs"] = kwargs
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session, "test prompt")

        args = captured_args["args"]
        assert args[0] == "claude"
        assert "-p" in args
        assert "test prompt" in args
        assert "--output-format" in args
        assert "stream-json" in args
        assert "--verbose" in args
        assert "--resume" not in args

    asyncio.run(_run())


def test_send_resume_uses_resume_flag():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"), resume_session_id="abc-123")
        session._started = True

        captured_args = {}
        fake = FakeProcess([_make_system_init("abc-123"), _make_result("abc-123")])

        async def fake_exec(*args, **kwargs):
            captured_args["args"] = args
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session, "follow up")

        args = captured_args["args"]
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "abc-123"

    asyncio.run(_run())


def test_send_with_model_override():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"), model="claude-sonnet-4-6")
        session._started = True

        captured_args = {}
        fake = FakeProcess([_make_system_init(), _make_result()])

        async def fake_exec(*args, **kwargs):
            captured_args["args"] = args
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session)

        args = captured_args["args"]
        assert "--model" in args
        idx = args.index("--model")
        assert args[idx + 1] == "claude-sonnet-4-6"

    asyncio.run(_run())


def test_session_id_captured_from_stream():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        fake = FakeProcess([_make_system_init("captured-uuid"), _make_result("captured-uuid")])

        with patch("asyncio.create_subprocess_exec", return_value=fake):
            await _collect_events(session)

        assert session.session_id == "captured-uuid"
        assert session._resume_session_id == "captured-uuid"

    asyncio.run(_run())


def test_yields_all_events():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        fake = FakeProcess([_make_system_init(), _make_assistant_text("World"), _make_result()])

        with patch("asyncio.create_subprocess_exec", return_value=fake):
            events = await _collect_events(session)

        assert len(events) == 3
        assert events[0]["type"] == "system"
        assert events[1]["type"] == "assistant"
        assert events[2]["type"] == "result"

    asyncio.run(_run())


def test_non_json_line_skipped():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        fake = FakeProcess(["not json", _make_system_init(), "bad line", _make_result()])

        with patch("asyncio.create_subprocess_exec", return_value=fake):
            events = await _collect_events(session)

        assert len(events) == 2
        assert events[0]["type"] == "system"
        assert events[1]["type"] == "result"

    asyncio.run(_run())


def test_nonzero_exit_yields_error():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        fake = FakeProcess([], returncode=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake):
            events = await _collect_events(session)

        assert len(events) == 1
        assert events[0]["type"] == "error"

    asyncio.run(_run())


def test_stop_terminates_process():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        session._process = mock_process

        await session.stop()
        mock_process.terminate.assert_called_once()

    asyncio.run(_run())


def test_stop_kills_on_timeout():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock(return_value=0)
        session._process = mock_process

        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await session.stop()

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()

    asyncio.run(_run())


def test_close_stops_and_resets():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True
        session._process = None

        await session.close()
        assert session._started is False

    asyncio.run(_run())


def test_cwd_set_to_repo_path():
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/my/repo"))
        session._started = True

        captured_kwargs = {}
        fake = FakeProcess([_make_result()])

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session)

        assert captured_kwargs["cwd"] == "/my/repo"

    asyncio.run(_run())


# --- Session-home isolation (CLAUDE_CONFIG_DIR) ---

def test_claude_config_dir_sets_env():
    """When claude_config_dir is set, env carries CLAUDE_CONFIG_DIR (resolved)
    plus the inherited os.environ keys."""
    async def _run():
        config_dir = Path("/my/repo/.claude-home")
        session = ClaudeCliSession(
            repo_path=Path("/my/repo"), claude_config_dir=config_dir
        )
        session._started = True

        captured_kwargs = {}
        fake = FakeProcess([_make_result()])

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch.dict(os.environ, {"PATH": "/inherited/path"}, clear=False):
                await _collect_events(session)

        env = captured_kwargs["env"]
        assert env is not None
        assert env["CLAUDE_CONFIG_DIR"] == str(config_dir.resolve())
        # Inherited os.environ keys are still present.
        assert env.get("PATH") == "/inherited/path"

    asyncio.run(_run())


def test_no_config_dir_inherits_parent_env():
    """When claude_config_dir is NOT set, env is None (parent inherited) —
    behavior unchanged from before."""
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/my/repo"))
        session._started = True

        captured_kwargs = {}
        fake = FakeProcess([_make_result()])

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session)

        assert captured_kwargs["env"] is None

    asyncio.run(_run())


# --- Per-run managed-key isolation (auth_env) ---

def test_auth_env_replaces_inherited_credential():
    """When auth_env is set, the subprocess gets that key and every inherited
    Claude credential is stripped — no os.environ key bleed across runs."""
    async def _run():
        session = ClaudeCliSession(
            repo_path=Path("/my/repo"),
            claude_config_dir=Path("/my/repo/.claude-home"),
            auth_env={"ANTHROPIC_API_KEY": "sk-run-specific"},
        )
        session._started = True

        captured_kwargs = {}
        fake = FakeProcess([_make_result()])

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        inherited = {
            "PATH": "/inherited/path",
            "ANTHROPIC_API_KEY": "sk-operator-default",
            "CLAUDE_CODE_OAUTH_TOKEN": "operator-oauth",
        }
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch.dict(os.environ, inherited, clear=False):
                await _collect_events(session)
                # os.environ itself is untouched — the env is built from a copy.
                assert os.environ["ANTHROPIC_API_KEY"] == "sk-operator-default"
                assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "operator-oauth"

        env = captured_kwargs["env"]
        assert env is not None
        # Non-auth inherited vars survive.
        assert env["PATH"] == "/inherited/path"
        # The run's key wins; the inherited OAuth token (which the CLI would
        # otherwise prefer) is gone.
        assert env["ANTHROPIC_API_KEY"] == "sk-run-specific"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        # Config dir still pinned.
        assert env["CLAUDE_CONFIG_DIR"] == str(Path("/my/repo/.claude-home").resolve())

    asyncio.run(_run())


def test_auth_env_without_config_dir_still_builds_env():
    """auth_env alone (no config dir) is enough to force an isolated env."""
    async def _run():
        session = ClaudeCliSession(
            repo_path=Path("/my/repo"),
            auth_env={"ANTHROPIC_API_KEY": "sk-only"},
        )
        session._started = True

        captured_kwargs = {}
        fake = FakeProcess([_make_result()])

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "op"}, clear=False):
                await _collect_events(session)

        env = captured_kwargs["env"]
        assert env is not None
        assert env["ANTHROPIC_API_KEY"] == "sk-only"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    asyncio.run(_run())


# --- Session-id pinning (--session-id) ---

def test_session_id_pins_fresh_session():
    """A fresh (non-resume) session with session_id set passes --session-id
    and pins it for subsequent resume."""
    async def _run():
        session = ClaudeCliSession(
            repo_path=Path("/tmp"), session_id="pinned-uuid-0001"
        )
        session._started = True

        captured_args = {}
        fake = FakeProcess([_make_system_init("pinned-uuid-0001"), _make_result("pinned-uuid-0001")])

        async def fake_exec(*args, **kwargs):
            captured_args["args"] = args
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session)

        args = captured_args["args"]
        assert "--session-id" in args
        idx = args.index("--session-id")
        assert args[idx + 1] == "pinned-uuid-0001"
        assert "--resume" not in args
        # Pinned id is set for resume on subsequent turns.
        assert session.session_id == "pinned-uuid-0001"
        assert session._resume_session_id == "pinned-uuid-0001"

    asyncio.run(_run())


def test_resume_wins_over_session_id():
    """When both resume_session_id and session_id are given, resume wins and
    --session-id is NOT passed."""
    async def _run():
        session = ClaudeCliSession(
            repo_path=Path("/tmp"),
            resume_session_id="resume-abc",
            session_id="pinned-uuid-0001",
        )
        session._started = True

        captured_args = {}
        fake = FakeProcess([_make_result("resume-abc")])

        async def fake_exec(*args, **kwargs):
            captured_args["args"] = args
            return fake

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _collect_events(session)

        args = captured_args["args"]
        assert "--resume" in args
        idx = args.index("--resume")
        assert args[idx + 1] == "resume-abc"
        assert "--session-id" not in args

    asyncio.run(_run())


# --- Deadlock regression: large stderr drained concurrently with stdout ---
#
# These tests spawn a REAL python helper (not a fake) that writes far more than
# the OS pipe buffer (~64KB) to stderr while emitting stream-json on stdout.
# Before the concurrent-drain fix, send() reads stdout to EOF then reads stderr
# only afterward -> the child blocks writing stderr while we block reading
# stdout -> permanent hang. asyncio.wait_for(..., timeout=15) catches that.

import sys  # noqa: E402


def _make_helper_script(stderr_bytes: int, exit_code: int) -> str:
    """A self-contained python program: emit stream-json to stdout, flood stderr."""
    return (
        "import sys\n"
        "sys.stdout.write('{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"deadlock-uuid\"}\\n')\n"
        "sys.stdout.write('{\"type\":\"assistant\",\"message\":{\"content\":"
        "[{\"type\":\"text\",\"text\":\"hi\"}]},\"session_id\":\"deadlock-uuid\"}\\n')\n"
        "sys.stdout.write('{\"type\":\"result\",\"subtype\":\"success\","
        "\"result\":\"hi\",\"session_id\":\"deadlock-uuid\"}\\n')\n"
        "sys.stdout.flush()\n"
        f"sys.stderr.write('E' * {stderr_bytes})\n"
        "sys.stderr.flush()\n"
        f"sys.exit({exit_code})\n"
    )


def _patch_real_helper(stderr_bytes: int, exit_code: int):
    """Patch create_subprocess_exec (in cli_session module) to launch the helper
    python program instead of `claude`, preserving stdout/stderr PIPEs."""
    script = _make_helper_script(stderr_bytes, exit_code)
    # Capture the real spawner BEFORE patching so the fake does not recurse into
    # the patched name.
    real_exec = asyncio.create_subprocess_exec

    async def _fake_exec(*_args, **kwargs):
        return await real_exec(
            sys.executable, "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=kwargs.get("cwd"),
            env=kwargs.get("env"),
        )

    return patch(
        "warden.providers.claude.cli_session.asyncio.create_subprocess_exec",
        new=_fake_exec,
    )


def test_large_stderr_does_not_deadlock_on_success():
    """>200KB on stderr + stream-json on stdout, exit 0: fully iterates, no hang."""
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        with _patch_real_helper(stderr_bytes=5_000_000, exit_code=0):
            events = await asyncio.wait_for(_collect_events(session, "x"), timeout=15)

        types = [e["type"] for e in events]
        assert types == ["system", "assistant", "result"]
        # Clean exit -> no error event appended.
        assert all(e["type"] != "error" for e in events)
        assert session.session_id == "deadlock-uuid"

    asyncio.run(_run())


def test_large_stderr_nonzero_exit_carries_stderr_no_hang():
    """>200KB on stderr, exit 1: error event carries stderr text, no hang."""
    async def _run():
        session = ClaudeCliSession(repo_path=Path("/tmp"))
        session._started = True

        with _patch_real_helper(stderr_bytes=5_000_000, exit_code=1):
            events = await asyncio.wait_for(_collect_events(session, "x"), timeout=15)

        assert events[-1]["type"] == "error"
        # The full drained stderr buffer feeds the error message — NOT the
        # generic "Claude CLI exited with code N" fallback (which would signal
        # the drain was cancelled/truncated before we read stderr_chunks).
        message = events[-1]["message"]
        assert "exited with code" not in message
        assert set(message) == {"E"}
        assert len(message) == 5_000_000
        # stdout events still fully delivered before the error event.
        assert [e["type"] for e in events[:-1]] == ["system", "assistant", "result"]

    asyncio.run(_run())
