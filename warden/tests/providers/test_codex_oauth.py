"""Tests for Codex OAuth CODEX_HOME isolation."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from warden.providers.codex.session import CodexSession


# --- Fake subprocess ---

class FakeProcess:
    def __init__(self):
        self.returncode = 0
        self.pid = 99999
        self.stdout = FakeStdout()
        self.stderr = FakeStderr()

    async def wait(self):
        return 0


class FakeStdout:
    def __init__(self):
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
        self._iter = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return (next(self._iter) + "\n").encode()
        except StopIteration:
            raise StopAsyncIteration


class FakeStderr:
    async def read(self):
        return b""


def test_codex_home_set_in_env():
    """Subprocess should receive CODEX_HOME when codex_home is provided."""
    async def _run():
        session = CodexSession(repo_path=Path("/tmp"), codex_home=Path("/custom/home"))
        session._started = True

        captured_kwargs = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return FakeProcess()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            async for _ in session.send("hello"):
                pass

        env = captured_kwargs.get("env")
        assert env is not None, "env should be set when codex_home is provided"
        assert env["CODEX_HOME"] == "/custom/home"

    asyncio.run(_run())


def test_codex_home_none_uses_default_env():
    """Subprocess should NOT have explicit env when codex_home is None."""
    async def _run():
        session = CodexSession(repo_path=Path("/tmp"))
        session._started = True

        captured_kwargs = {}

        async def fake_exec(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return FakeProcess()

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            async for _ in session.send("hello"):
                pass

        assert captured_kwargs.get("env") is None, "env should be None (inherit system) when no codex_home"

    asyncio.run(_run())


def test_codex_home_relative_path_resolved_to_absolute():
    """Relative codex_home path should be resolved to absolute."""
    session = CodexSession(repo_path=Path("/tmp"), codex_home=Path("relative/path"))
    assert session._codex_home is not None
    assert session._codex_home.is_absolute()


def test_codex_session_import_ok():
    """Verify CodexSession still imports correctly after changes."""
    from warden.providers.codex.session import CodexSession
    s = CodexSession(repo_path=Path("/tmp"))
    assert s._codex_home is None
