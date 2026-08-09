"""Claude CLI subprocess provider.

Wraps `claude -p` as a subprocess, streaming NDJSON events.
Follows the same lifecycle pattern as CodexSession.
"""

import asyncio
import json
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from warden.providers.auth import PROVIDER_AUTH_VARS

logger = logging.getLogger(__name__)


class ClaudeCliSession:
    """Wraps the Claude CLI as a subprocess-based agent session."""

    #: Stable provider key the resume path matches on (not the class name).
    PROVIDER = "claude-cli"

    def __init__(
        self,
        repo_path: Path,
        model: str | None = None,
        resume_session_id: str | None = None,
        claude_config_dir: Path | None = None,
        session_id: str | None = None,
        auth_env: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        self.session_id: str | None = resume_session_id
        self.repo_path = Path(repo_path)
        self._model = model
        self._process: asyncio.subprocess.Process | None = None
        self._resume_session_id = resume_session_id
        self._started = False
        self.jsonl_path: str | None = None
        # Pin the agent home inside the task folder so the transcript is
        # self-contained. None => inherit parent env (unchanged behavior).
        self._claude_config_dir: Path | None = (
            Path(claude_config_dir).resolve() if claude_config_dir else None
        )
        # Per-run managed-key isolation. When set, the subprocess env drops any
        # inherited Claude credential and uses only these vars — so concurrent
        # runs can each carry a different key with no os.environ bleed. None =>
        # inherit the parent credential (unchanged single-key behavior).
        self._auth_env: dict[str, str] | None = auth_env
        # Optional UUID to pin a NEW session's id (deterministic ids). Only
        # applied on the first (non-resume) turn; resume always wins.
        self._pin_session_id: str | None = session_id

    async def start(self) -> None:
        """Validate Claude CLI is installed."""
        if not shutil.which("claude"):
            raise RuntimeError(
                "Claude CLI not found. Install from: https://docs.anthropic.com/en/docs/claude-code"
            )
        self._started = True
        logger.info(
            "ClaudeCliSession started (session_id=%s, cwd=%s, model=%s)",
            self.session_id, self.repo_path, self._model,
        )

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Send a prompt to Claude CLI and yield NDJSON events.

        First turn creates a new session. Subsequent turns resume via --resume.
        """
        if not self._started:
            raise RuntimeError("ClaudeCliSession not started")

        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]

        # Resume existing session
        if self._resume_session_id:
            cmd.extend(["--resume", self._resume_session_id])
        elif self._pin_session_id:
            # Fresh session with a caller-pinned id. Pin it now so subsequent
            # turns resume the same id. --session-id accepts a UUID for a NEW
            # session and must NOT be combined with --resume.
            cmd.extend(["--session-id", self._pin_session_id])
            self.session_id = self._pin_session_id
            self._resume_session_id = self._pin_session_id

        # Model override
        if self._model:
            cmd.extend(["--model", self._model])

        # Build the subprocess env. env=None inherits the parent process env
        # (auth tokens included) — same as today when neither a config dir nor a
        # per-run key is set.
        env = None
        if self._claude_config_dir is not None or self._auth_env is not None:
            env = {**os.environ}
            if self._auth_env is not None:
                # Per-run key isolation: drop every inherited Claude credential
                # first so the operator's os.environ key can't shadow the
                # injected managed key (the CLI prefers OAuth over API key), then
                # apply only this run's auth vars.
                for var in PROVIDER_AUTH_VARS["claude-cli"]:
                    env.pop(var, None)
                env.update(self._auth_env)
            if self._claude_config_dir is not None:
                env["CLAUDE_CONFIG_DIR"] = str(self._claude_config_dir)

        logger.info("[claude-cli] send() cmd=%s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.repo_path),
            env=env,
        )
        logger.info(
            "[claude-cli] subprocess spawned pid=%s session=%s",
            self._process.pid, self.session_id,
        )

        assert self._process.stdout is not None

        # Drain stderr CONCURRENTLY with stdout. If the child writes more than
        # the OS pipe buffer (~64KB) to stderr before exiting, it would block
        # on the stderr write while we block reading stdout -> deadlock. Reading
        # stderr in a background task keeps its pipe empty so the child never
        # blocks on it.
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert self._process is not None
            assert self._process.stderr is not None
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())

        try:
            async for event in self._read_stdout_events():
                yield event
        except BaseException:
            # Exception / consumer cancellation: we're bailing early, so the
            # drain may still be mid-read. Cancel it so it can't leak, then
            # re-raise. (Normal completion falls through and awaits it below.)
            if not stderr_task.done():
                stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
            raise

        # Normal flow: stdout EOF'd. stderr may still be draining independently
        # (on a non-zero exit stdout can EOF first), so let the drain finish
        # BEFORE reading stderr_chunks — otherwise the error event below could
        # be truncated/empty and mask the real error with the generic fallback.
        await stderr_task

        logger.info(
            "[claude-cli] stdout finished session=%s",
            self.session_id,
        )
        await self._process.wait()
        logger.info(
            "[claude-cli] process exited rc=%s session=%s",
            self._process.returncode, self.session_id,
        )

        if self._process.returncode != 0:
            stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()
            logger.error("[claude-cli] stderr: %s", stderr_text[:500])
            yield {
                "type": "error",
                "message": stderr_text or f"Claude CLI exited with code {self._process.returncode}",
            }

        self._process = None

    async def _read_stdout_events(self) -> AsyncGenerator[Any, None]:
        """Read and parse NDJSON events from the subprocess stdout stream."""
        assert self._process is not None
        assert self._process.stdout is not None
        buffer = ""
        line_count = 0

        while True:
            chunk = await self._process.stdout.readline()
            if not chunk:
                # Handle any remaining buffered data
                if buffer.strip():
                    try:
                        event = json.loads(buffer.strip())
                        line_count += 1
                        self._capture_session_id(event)
                        yield event
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON trailing buffer: %s", buffer[:200])
                break

            line_str = chunk.decode()
            buffer += line_str

            # Try to parse complete JSON lines from buffer
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                line_count += 1
                try:
                    event = json.loads(line)
                    self._capture_session_id(event)
                    if line_count <= 5 or line_count % 20 == 0:
                        logger.info(
                            "[claude-cli] session=%s line#%d type=%s",
                            self.session_id, line_count, event.get("type", "?"),
                        )
                    yield event
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line from Claude CLI: %s", line[:200])

        logger.info(
            "[claude-cli] stdout stream ended session=%s total_lines=%d",
            self.session_id, line_count,
        )

    def _capture_session_id(self, event: dict) -> None:
        """Extract session_id from system/init or result events."""
        if self.session_id:
            return
        sid = event.get("session_id")
        if sid:
            self.session_id = sid
            # Once captured, set for resume on subsequent turns
            self._resume_session_id = sid
            logger.info("[claude-cli] captured session_id=%s", sid)

    async def stop(self) -> None:
        """Terminate the running Claude CLI subprocess."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    async def close(self) -> None:
        """Stop and clean up."""
        await self.stop()
        self._started = False
        logger.info("ClaudeCliSession %s closed", self.session_id)
