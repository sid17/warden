"""Codex Python SDK adapter — ``CodexSdkSession`` (subsumes ``codex exec``).

The canonical Codex provider, built on the official ``openai-codex`` SDK
(v0.144.4). Replaces the reduced ``codex exec`` subprocess adapter
(``codex/session.py``, kept mv-only, gated at the factory) with a full-contract
adapter: OpenAI models + ChatGPT-OAuth (``CODEX_HOME/auth.json``) / API key, and
**fail-closed exec/patch permission gating** wired into the harness three-stage
chain via the SDK's approval seam.

Explicitly OUT of provider scope (contract A.1): SESS-3 snapshot (orchestrator)
and SAFE-1 output filtering (core drain pass).

## The verified plumbing (do NOT swap to the async client)

Approvals work ONLY on the low-level sync ``CodexClient(config,
approval_handler=...)`` (client.py:215-221). The high-level ``Codex`` builds its
own client with the fail-OPEN default handler, so we construct ``Codex(config)``
(it starts + initializes in its ctor) and then INJECT our handler onto
``codex._client._approval_handler``. The handler is SYNC and runs on the SDK's
reader THREAD; our ``can_use_tool`` is async on the orchestrator loop, so the
handler bridges via ``asyncio.run_coroutine_threadsafe(...).result(timeout)`` and
FAILS CLOSED (decline) on any exception/timeout.

The whole client is blocking (subprocess + threads), so every blocking call
(ctor, ``thread_start``, turn iteration) runs OFF the loop via
``asyncio.to_thread``; ``TurnHandle.stream()`` (a sync ``Iterator``) is pumped
from a worker thread into an ``asyncio.Queue`` that the async ``send()`` drains.

## The load-bearing approval finding (empirical, 2026-07-18)

``ApprovalMode.auto_review`` maps to ``AskForApproval(on_request)`` +
``ApprovalsReviewer.auto_review`` — and the auto-reviewer AUTO-APPROVES exec/patch
BEFORE our handler is consulted (verified: handler fired 0 times, file written).
The fix (verified to make the handler load-bearing under both sandboxes): start
the thread with ``approval_policy=untrusted`` and ``approvals_reviewer=None``.
Under that policy the handler FIRES for every command/file-change escalation, a
``decline`` BLOCKS the side effect, and an ``accept`` lets it run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from warden.config.models import AuditConfig
from warden.providers.base_provider import BaseProvider
from warden.providers.codex.audit_tap import CodexAuditTap
from warden.providers.codex.custom_tool_mcp_server import CustomToolMcpServer
from warden.providers.codex.sdk_message_handler import (
    approval_to_tool_call,
    notification_to_event,
)
from warden.seams.custom_tools import CustomTool

logger = logging.getLogger(__name__)

# How long the sync approval handler waits for the async can_use_tool decision
# before failing CLOSED (decline). Codex blocks the tool until we answer.
_APPROVAL_TIMEOUT_S = 30.0

# Approval server-request method codex uses for MCP tool calls. This is the
# ELICITATION path (verified) — its response shape is the MCP elicit-result
# ``{"action": "accept"|"decline"}``, NOT the exec/patch ``{"decision": …}``.
# MCP custom tools are delivered UNGATED behind an explicit opt-in, so this is
# auto-accepted only when ``allow_ungated_custom_tools`` is set.
_MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"

# The FastMCP server name codex references in the injected mcp_servers config.
_MCP_SERVER_NAME = "harness_custom"

# Sentinel pushed onto the queue by the streaming worker when the turn ends.
_STREAM_DONE = object()


class CodexSdkSession(BaseProvider):
    """Codex provider on the official ``openai-codex`` SDK (see module docstring)."""

    #: Stable provider key the resume path matches on (not the class name —
    #: the stale ``CodexSession`` map value was the root of bug 4a / N9).
    PROVIDER = "codex"

    # --- Capability flags (design-coverage §4 / sdk-reality) ------------------
    crash_isolated = True  # SDK shells the `codex` binary as a subprocess
    hard_kill_tier = "os"  # harness can OS-kill the client subprocess
    cost_visibility = "coarse"  # usage only on the terminal TurnResult (C5)
    compaction = "harness_driven"  # thread.compact() at split points (C9)
    supports_hard_deadline = True  # true OS kill of a harness-owned PID
    perm_tier = "arg_level"  # handler ON: exec/patch fail-closed with args
    retry_owner = "sdk"  # the openai/codex SDK retries transient errors (C4)
    max_output_tokens = None  # C6: SDK/native compaction manages the window

    def describe_auth(self) -> dict:
        """C7/AUTH-3: report auth mode + fingerprint (never the key)."""
        from warden.providers.auth import describe_auth
        return describe_auth("codex", self._auth_env)
    # NOTE: `custom_tool_delivery` is an INSTANCE property (below) — "mcp" when the
    # ungated opt-in is set (tools present), else "none". exec/patch gating is
    # unaffected by the opt-in.

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
        approval_mode: str | None = None,
        allow_ungated_custom_tools: bool = False,
        audit: "AuditConfig | None" = None,
        **kwargs: Any,
    ):
        self._reject_unknown_kwargs(kwargs)
        # Codex has no native PreToolUse/PostToolUse hooks; the audit trail is
        # DERIVED from the normalized event stream (M5 3b). None => off.
        self._audit_tap = CodexAuditTap.create(audit)
        # Codex custom-tool delivery is UNGATED (MCP calls ride the elicitation
        # path, which cannot carry a can_use_tool decision — verified). So custom
        # tools require the explicit `allow_ungated_custom_tools` opt-in. Default
        # (opt-in off) stays fail-closed: passing custom_tools RAISES at
        # construction (TOOL-1: consume-or-error, never silently drop). The in-proc
        # MCP server is then LAUNCHED in start() (opt-in on).
        self._allow_ungated_custom_tools = allow_ungated_custom_tools
        if custom_tools and not allow_ungated_custom_tools:
            raise NotImplementedError(
                "CodexSdkSession received custom_tools but "
                "allow_ungated_custom_tools is False. Codex custom tools cannot be "
                "permission-gated (they ride the MCP elicitation path; can_use_tool "
                "is never consulted for them), so they are delivered UNGATED and "
                "require the explicit `allow_ungated_custom_tools=True` opt-in. "
                "exec/patch gating is unaffected and stays fail-closed."
            )
        # Session id is None until captured from the started thread, or pinned on
        # resume/factory passthrough.
        self.session_id: str | None = resume_session_id or session_id
        self.repo_path = Path(repo_path)
        self._can_use_tool = can_use_tool
        self._model = model
        self._resume_session_id = resume_session_id
        self._pin_session_id = session_id
        self._disallowed_tools = disallowed_tools or []
        self._system_prompt = system_prompt
        # Per-run managed-key isolation (B8/N3): strip inherited codex creds then
        # inject these. None => inherit ambient env (single-user path unchanged).
        self._auth_env: dict[str, str] | None = auth_env
        # Pin the agent home (C4) — OAuth auth.json + transcripts read from here.
        self._codex_home: Path | None = (
            Path(codex_home).resolve() if codex_home else None
        )
        # approval_mode is accepted for parity with the typed input set. Codex's
        # public ApprovalMode.auto_review AUTO-APPROVES before our handler (see
        # module docstring), so the adapter ALWAYS starts the thread with the
        # empirically-verified fail-closed policy (untrusted + no auto-reviewer)
        # regardless of this hint. Stored so nothing silently drops.
        self._approval_mode = approval_mode
        self._custom_tools: list[CustomTool] = custom_tools or []

        self._codex: Any = None  # openai_codex.Codex (owns the sync CodexClient)
        self._thread: Any = None  # openai_codex.api.Thread
        self._active_handle: Any = None  # current openai_codex.api.TurnHandle
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self.jsonl_path: str | None = None
        # In-proc streamable-HTTP MCP server for ungated custom-tool delivery.
        # Built in start() only when tools are present AND the opt-in is set.
        self._mcp_server: CustomToolMcpServer | None = None
        self._mcp_url: str | None = None

    # --- capability (instance) -----------------------------------------------

    @property
    def custom_tool_delivery(self) -> str:
        """`"mcp"` when custom tools are delivered via the in-proc MCP server
        (opt-in set + tools present), else `"none"`. exec/patch gating is
        independent of this."""
        if self._custom_tools and self._allow_ungated_custom_tools:
            return "mcp"
        return "none"

    # --- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Build the sync Codex client (off-loop), inject the fail-closed approval
        handler, authenticate, and start (or resume) the thread."""
        if self._started:
            raise RuntimeError(f"CodexSdkSession {self.session_id} already started")

        # Capture the orchestrator loop so the reader-thread handler can bridge to
        # our async can_use_tool via run_coroutine_threadsafe (module docstring).
        self._loop = asyncio.get_running_loop()

        # Custom-tool delivery decision (fail-closed by default). Codex custom
        # tools ride the MCP elicitation path and CANNOT be permission-gated
        # (can_use_tool is never consulted for them), so they are only delivered
        # when the caller explicitly opts in.
        await self._prepare_custom_tools()

        # Build the process env: inherit, strip inherited codex creds + inject
        # this run's key (B8/N3), and pin CODEX_HOME (OAuth auth.json + C4).
        env = dict(os.environ)
        env = self.apply_auth_env(env, "codex", self._auth_env)
        if self._codex_home is not None:
            env["CODEX_HOME"] = str(self._codex_home)

        # Codex() starts + initializes the client in its ctor (blocking) → to_thread.
        self._codex = await asyncio.to_thread(self._build_codex, env)
        # Inject OUR fail-closed handler onto the internal sync client. The
        # high-level Codex constructed it with the fail-OPEN default; overriding
        # the attribute is the only seam (Codex/CodexConfig expose no handler).
        self._codex._client._approval_handler = self._approval

        # API-key mode: explicit login when a key was injected. OAuth mode reads
        # CODEX_HOME/auth.json directly (no explicit login).
        api_key = (self._auth_env or {}).get("OPENAI_API_KEY") or env.get("OPENAI_API_KEY")
        if self._auth_env and self._auth_env.get("OPENAI_API_KEY"):
            await asyncio.to_thread(self._codex.login_api_key, api_key)

        # Start (or resume) the thread under the fail-closed approval policy.
        self._thread = await asyncio.to_thread(self._start_thread)
        self.session_id = self._thread.id
        self._started = True
        logger.info(
            "CodexSdkSession started thread=%s cwd=%s (auth=%s)",
            self.session_id, self.repo_path,
            "api-key" if api_key else "oauth/inherited",
        )

    async def _prepare_custom_tools(self) -> None:
        """Launch the in-proc custom-tool MCP server (opt-in path only).

        The fail-closed gate (tools present + opt-in OFF → raise naming the flag)
        already fired at construction. Here: no tools OR opt-in OFF → no-op; tools
        present + opt-in ON → start the in-proc streamable-HTTP MCP server, capture
        its URL (injected into the codex thread config in ``_build_codex``), and
        log a LOUD warning that these tools BYPASS permission gating.
        """
        if self.custom_tool_delivery != "mcp":
            return
        self._mcp_server = CustomToolMcpServer(
            self._custom_tools, server_name=_MCP_SERVER_NAME
        )
        self._mcp_url = await self._mcp_server.start()
        logger.warning(
            "!!! UNGATED CUSTOM TOOLS !!! CodexSdkSession is delivering %d custom "
            "tool(s) to codex via an in-proc MCP server (%s). These tools BYPASS "
            "permission gating (can_use_tool is NOT consulted for them) — opt-in "
            "allow_ungated_custom_tools=True. Tools: %s. exec/patch remain gated.",
            len(self._custom_tools), self._mcp_url,
            [t.name for t in self._custom_tools],
        )

    def _build_codex(self, env: dict[str, str]) -> Any:
        """Construct the high-level ``Codex`` (blocking ctor). Isolated for
        ``to_thread`` + so tests can stub it.

        When ungated custom tools are wired, inject the in-proc MCP server as a
        streamable-HTTP ``mcp_servers`` entry via ``config_overrides`` (the exact
        TOML-dotted shape ``codex mcp add --url`` writes; verified in the phase-3
        SDK-reality note)."""
        from openai_codex import Codex
        from openai_codex.client import CodexConfig

        overrides: tuple[str, ...] = ()
        if self._mcp_url is not None:
            overrides = (
                f'mcp_servers.{_MCP_SERVER_NAME}.url="{self._mcp_url}"',
            )
        config = CodexConfig(
            cwd=str(self.repo_path), env=env, config_overrides=overrides
        )
        return Codex(config)

    def _start_thread(self) -> Any:
        """Start or resume the thread with the EMPIRICALLY-verified fail-closed
        approval policy: ``approval_policy=untrusted`` + ``approvals_reviewer=None``
        (so our handler is consulted for every exec/patch, not auto-approved)."""
        from openai_codex import Sandbox
        from openai_codex.api import Thread
        from openai_codex._sandbox import _sandbox_mode
        from openai_codex.generated.v2_all import (
            AskForApproval,
            AskForApprovalValue,
            ThreadStartParams,
            ThreadResumeParams,
        )

        client = self._codex._client
        policy = AskForApproval(root=AskForApprovalValue.untrusted)
        if self._resume_session_id:
            params = ThreadResumeParams(
                thread_id=self._resume_session_id,
                approval_policy=policy,
                approvals_reviewer=None,
                sandbox=_sandbox_mode(Sandbox.workspace_write),
                cwd=str(self.repo_path),
                model=self._model,
            )
            resumed = client.thread_resume(self._resume_session_id, params)
            return Thread(client, resumed.thread.id)

        params = ThreadStartParams(
            approval_policy=policy,
            approvals_reviewer=None,
            sandbox=_sandbox_mode(Sandbox.workspace_write),
            cwd=str(self.repo_path),
            model=self._model,
            base_instructions=self._system_prompt,
        )
        started = client.thread_start(params)
        return Thread(client, started.thread.id)

    # --- turn / event stream -------------------------------------------------

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Run one turn off the loop and stream its normalized events.

        Starts the turn + pumps the sync ``TurnHandle.stream()`` from a worker
        thread into an ``asyncio.Queue`` that this coroutine drains and yields.
        """
        if not self._started or self._thread is None:
            raise RuntimeError("CodexSdkSession not started")

        handle = await asyncio.to_thread(self._thread.turn, prompt)
        self._active_handle = handle
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _pump() -> None:
            try:
                for notification in handle.stream():
                    loop.call_soon_threadsafe(queue.put_nowait, notification)
            except BaseException as exc:  # noqa: BLE001 — forward, never swallow
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        pump_task = asyncio.create_task(asyncio.to_thread(_pump))
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    break
                if isinstance(item, BaseException):
                    logger.exception("Codex stream error", exc_info=item)
                    yield {
                        "kind": "error",
                        "sessionId": self.session_id,
                        "text": str(item),
                        "isError": True,
                    }
                    break
                event = notification_to_event(item, self.session_id or "")
                if event is not None:
                    if self._audit_tap is not None:
                        self._audit_tap.record(event)
                    yield event
        finally:
            self._active_handle = None
            await pump_task

    # --- permission bridge (SYNC, runs on the SDK reader thread) --------------

    def _approval(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """SYNC approval handler invoked on the SDK reader thread. Bridges to the
        async ``can_use_tool`` on the orchestrator loop; FAILS CLOSED (decline)
        on unknown method / no callback / exception / timeout.

        UNGATED custom-tool path: codex MCP tool calls arrive as
        ``mcpServer/elicitation/request`` (the elicitation path, NOT exec/patch).
        When ungated custom tools are opted-in, auto-accept those with the MCP
        elicit-result shape ``{"action": "accept"}`` — WITHOUT consulting
        ``can_use_tool`` (that is the "ungated" contract). If not opted-in, this
        method is unknown and falls through to fail-closed decline. exec/patch
        approvals below are unaffected and stay fail-closed."""
        if method == _MCP_ELICITATION_METHOD:
            if self.custom_tool_delivery == "mcp":
                logger.warning(
                    "Codex approval: MCP custom-tool elicitation → ACCEPT (UNGATED; "
                    "can_use_tool NOT consulted, opt-in allow_ungated_custom_tools)"
                )
                return {"action": "accept"}
            # Not opted-in → decline the elicitation (fail-closed).
            logger.warning(
                "Codex approval: MCP elicitation without ungated opt-in → decline"
            )
            return {"action": "decline"}

        mapping = approval_to_tool_call(method, params)
        if mapping is None:
            logger.warning("Codex approval: unknown method %s → decline", method)
            return {"decision": "decline"}
        tool_name, tool_input = mapping

        if self._can_use_tool is None or self._loop is None:
            # No handler wired = reduced profile; the harness always supplies one,
            # so a missing callback here is a fail-closed condition (PERM-3).
            logger.warning("Codex approval: no can_use_tool wired → decline")
            return {"decision": "decline"}

        try:
            # pre-07b / 3a — surface the approval's per-call id (item_id, captured
            # into tool_input by approval_to_tool_call) via the context the seam
            # extracts, so a Codex built-in consult is identified (case #3).
            seam_ctx = {"item_id": tool_input.get("item_id")}
            fut = asyncio.run_coroutine_threadsafe(
                self._can_use_tool(tool_name, tool_input, seam_ctx), self._loop
            )
            result = fut.result(timeout=_APPROVAL_TIMEOUT_S)
        except Exception:
            logger.exception("Codex approval bridge failed → decline (fail-closed)")
            return {"decision": "decline"}

        behavior = getattr(result, "behavior", None)
        decision = "accept" if behavior == "allow" else "decline"
        logger.info(
            "Codex approval %s tool=%s → %s", method, tool_name, decision
        )
        return {"decision": decision}

    # --- teardown ------------------------------------------------------------

    async def stop(self) -> None:
        """Interrupt the active turn (best-effort), off the loop."""
        handle = getattr(self, "_active_handle", None)
        if handle is not None:
            try:
                await asyncio.to_thread(handle.interrupt)
            except Exception:
                logger.exception("Error interrupting codex turn %s", self.session_id)

    def discover_jsonl_path(self) -> None:
        """Populate ``jsonl_path`` with this thread's on-disk rollout transcript.

        Parity with ``ClaudeSession``/``OpenHarnessSession`` (contract S8 —
        transcript addressable). Codex writes the rollout at
        ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl``; the
        thread id IS our ``session_id`` (``sdk_session`` captures it from the
        started thread). We match by that EXACT id and take NO "most recent"
        fallback — a wrong transcript is worse than a null pointer.

        The home is resolved the SAME way the subprocess picks it: the pinned
        ``_codex_home`` if set, else the ambient ``CODEX_HOME`` env (what an
        un-pinned turn actually inherits — e.g. the Docker bed), else
        ``~/.codex``. Using the pinned/ambient home (not blindly ``~/.codex``)
        keeps a shared home from leaking another session's file.

        Zero-arg + idempotent (the orchestrator calls it post-stream only when
        ``jsonl_path`` is unset). Fail-soft (LAW 4: log, never crash the turn).
        """
        if self.jsonl_path or not self.session_id:
            return
        env_home = os.environ.get("CODEX_HOME")
        home = self._codex_home or (Path(env_home) if env_home else (Path.home() / ".codex"))
        root = home / "sessions"
        if not root.exists():
            return
        try:
            # rollout-<ts>-<thread_id>.jsonl → match the exact thread id suffix.
            matches = sorted(root.rglob(f"*{self.session_id}.jsonl"))
            if matches:
                self.jsonl_path = str(matches[0])
                logger.info(
                    "CodexSdkSession %s discovered JSONL at %s",
                    self.session_id, self.jsonl_path,
                )
        except OSError:
            logger.debug(
                "Could not discover JSONL path for CodexSdkSession %s",
                self.session_id,
            )

    async def close(self) -> None:
        """OS-kill / tear down the client subprocess (hard_kill_tier=os), then the
        in-proc custom-tool MCP server (if one was started)."""
        await self.stop()
        if self._codex is not None:
            try:
                await asyncio.to_thread(self._codex.close)
            except Exception:
                logger.exception("Error closing codex client %s", self.session_id)
            finally:
                self._codex = None
                self._thread = None
                self._started = False
                logger.info("CodexSdkSession %s closed", self.session_id)
        if self._mcp_server is not None:
            try:
                await self._mcp_server.stop()
            except Exception:
                logger.exception(
                    "Error stopping custom-tool MCP server %s", self.session_id
                )
            finally:
                self._mcp_server = None
                self._mcp_url = None
