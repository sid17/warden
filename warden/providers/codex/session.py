import asyncio
import glob
import json
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from warden.providers.base_provider import BaseProvider
from warden.seams.custom_tools import CustomTool

logger = logging.getLogger(__name__)


class CodexSession(BaseProvider):
    """Wraps the Codex CLI as a subprocess-based agent session.

    Adopts the uniform provider contract in Phase 1 (accepts the full standard
    typed input set + rejects unknown kwargs + declares its capability flags).
    Its capability IMPLEMENTATION — arg-level permissions, custom-tool
    consumption — is Phase-2 work; today the reduced ``codex exec`` adapter has
    ``perm_tier="none"`` and ``custom_tool_delivery="none"``.
    """

    # --- Capability flags (design-coverage §4) -------------------------------
    crash_isolated = True  # harness-owned subprocess
    hard_kill_tier = "os"  # harness owns the PID → SIGKILL
    cost_visibility = "coarse"  # mid-run cumulative session totals
    compaction = "harness_driven"
    supports_hard_deadline = True  # clean OS kill
    custom_tool_delivery = "none"  # D6 — MCP-tool path parked; consume-or-error
    perm_tier = "none"  # reduced `codex exec` profile today (→ arg_level Phase 2)
    retry_owner = "sdk"  # codex CLI/SDK owns transient retries (C4)
    max_output_tokens = None  # C6: SDK-managed window

    def __init__(
        self,
        repo_path: Path,
        can_use_tool: Any = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        auth_env: dict[str, str] | None = None,
        custom_tools: list[CustomTool] | None = None,
        codex_home: Path | None = None,
        **kwargs: Any,
    ):
        self._reject_unknown_kwargs(kwargs)
        # Belt-and-suspenders alongside the factory C1 guard: Codex cannot yet
        # consume custom tools, so it errors rather than silently dropping them.
        if custom_tools:
            raise NotImplementedError(
                "CodexSession does not support custom_tools yet"
            )
        # Session ID is None until captured from Codex's thread.started event,
        # or set upfront if resuming a known session.
        self.session_id: str | None = resume_session_id or session_id
        self.repo_path = Path(repo_path)
        self._model = model or "gpt-5.4"
        self._process: asyncio.subprocess.Process | None = None
        # If resuming, pre-set thread_id so first send() resumes the thread
        self._thread_id: str | None = resume_session_id
        self._started = False
        self.jsonl_path: str | None = None
        self._codex_home: Path | None = Path(codex_home).resolve() if codex_home else None
        # Accepted-and-stored even where unused for the reduced `codex exec`
        # profile (no can_use_tool callback / no arg-level perms yet) — Phase 2
        # wires these. Stored so nothing silently drops (the contract) and so
        # Phase 2 has the values without a signature change.
        self._can_use_tool = can_use_tool
        self._resume_session_id = resume_session_id
        self._disallowed_tools = disallowed_tools or []
        self._system_prompt = system_prompt
        # NOTE: auth_env is accepted (closes the kwargs silent-drop) but NOT
        # stored on self and NOT injected — Codex per-run auth is Phase-2/N3
        # work. Storing it here would falsely imply it is wired. Phase 2 adds
        # `self._auth_env` + the strip-then-inject at the subprocess env point.
        self._custom_tools: list[CustomTool] = custom_tools or []

    async def start(self) -> None:
        """Validate Codex CLI is installed."""
        if not shutil.which("codex"):
            raise RuntimeError(
                "Codex CLI not found. Install with: npm install -g @openai/codex"
            )
        self._started = True
        logger.info("CodexSession %s started (cwd=%s, model=%s)", self.session_id, self.repo_path, self._model)

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Send a prompt to Codex CLI and yield JSON events.

        First turn creates a new thread via `codex exec`. Subsequent turns
        resume the same thread via `codex exec resume <thread_id>` so the
        agent retains full conversation history across turns.
        """
        if not self._started:
            raise RuntimeError(f"CodexSession {self.session_id} not started")

        if self._thread_id:
            cmd = [
                "codex", "exec", "resume",
                self._thread_id, prompt,
                "--json",
                "-m", self._model,
            ]
        else:
            cmd = [
                "codex", "exec",
                "-m", self._model,
                "-s", "workspace-write",
                "--json",
                "-C", str(self.repo_path),
                prompt,
            ]

        print(f"[DIAG][codex] send() cmd={' '.join(cmd)}")
        env = None
        if self._codex_home is not None:
            env = {**os.environ, "CODEX_HOME": str(self._codex_home)}
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        print(f"[DIAG][codex] subprocess spawned pid={self._process.pid} for session={self.session_id}")

        assert self._process.stdout is not None
        line_count = 0
        async for line in self._process.stdout:
            line_str = line.decode().strip()
            if not line_str:
                continue
            line_count += 1
            try:
                event = json.loads(line_str)
                event_type = event.get("type", "unknown")
                if line_count <= 10 or line_count % 20 == 0:
                    print(f"[DIAG][codex] session={self.session_id} line#{line_count} event_type={event_type}")
                if event_type == "thread.started" and not self._thread_id:
                    self._thread_id = event.get("thread_id")
                    self.session_id = self._thread_id
                    print(f"[DIAG][codex] captured thread_id={self._thread_id}, set as session_id")
                # Capture JSONL path from session_meta event
                if event_type == "session_meta" and not self.jsonl_path:
                    print(f"[DIAG][codex] session_meta event: {json.dumps(event)[:500]}")
                    self.discover_jsonl_path(event)
                yield event
            except json.JSONDecodeError:
                logger.warning("Non-JSON line from Codex: %s", line_str[:200])

        print(f"[DIAG][codex] stdout finished for session={self.session_id}, total_lines={line_count}")
        await self._process.wait()
        print(f"[DIAG][codex] process exited rc={self._process.returncode} for session={self.session_id}")
        if self._process.returncode != 0:
            assert self._process.stderr is not None
            stderr_text = (await self._process.stderr.read()).decode().strip()
            print(f"[DIAG][codex] stderr: {stderr_text[:500]}")
            yield {"type": "error", "message": stderr_text or f"Codex exited with code {self._process.returncode}"}

        self._process = None

    def discover_jsonl_path(self, session_meta: dict | None = None) -> None:
        """Discover the JSONL path from the session_meta event or filesystem."""
        if self.jsonl_path:
            return
        try:
            # If we have the session_meta event, extract the UUID and glob for it
            if session_meta:
                payload = session_meta.get("payload", {})
                codex_id = payload.get("id", "")
                if codex_id:
                    home = Path.home()
                    pattern = str(home / ".codex" / "sessions" / "**" / f"*{codex_id}.jsonl")
                    matches = glob.glob(pattern, recursive=True)
                    if matches:
                        self.jsonl_path = matches[0]
                        logger.info("CodexSession %s found JSONL at %s", self.session_id, self.jsonl_path)
                        return

            # Fallback: find the most recently modified JSONL in Codex sessions dir
            home = Path.home()
            pattern = str(home / ".codex" / "sessions" / "**" / "*.jsonl")
            matches = glob.glob(pattern, recursive=True)
            if matches:
                matches.sort(key=lambda p: Path(p).stat().st_mtime, reverse=True)
                # Take the most recent file (likely ours)
                self.jsonl_path = matches[0]
                logger.info("CodexSession %s fallback JSONL at %s", self.session_id, self.jsonl_path)
        except Exception:
            logger.debug("Could not discover JSONL path for CodexSession %s", self.session_id)

    async def stop(self) -> None:
        """Terminate the running Codex subprocess."""
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
        logger.info("CodexSession %s closed", self.session_id)
