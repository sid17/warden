import inspect
import json
import logging
import re
import time
import warnings
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    create_sdk_mcp_server,
    tool,
)

from warden.config.models import AuditConfig, PathHookConfig, TelemetryConfig
from warden.providers.claude.continuation_hook import (
    install_continuation_hook,
    make_tracker,
    observe_completion,
)
from warden.providers.base_provider import BaseProvider
from warden.seams.custom_tools import CustomTool

# MCP server name the custom tools are registered under. Tools are reachable to
# the model as ``mcp__{server}__{tool_name}`` (SDK-MCP fully-qualified name).
_CUSTOM_MCP_SERVER = "harness_custom"

#: Fully-qualified prefix of every custom tool; the callback's guard prefix.
_CUSTOM_TOOL_PREFIX = f"mcp__{_CUSTOM_MCP_SERVER}__"
#: SDK ``HookMatcher`` regex that scopes the gate to our custom tools only.
_CUSTOM_TOOL_PREFIX_RE = f"^{_CUSTOM_TOOL_PREFIX}"

#: Ceiling on a single JSON control message on the CLI↔SDK stdin channel. The SDK
#: defaults to 1 MB (``_DEFAULT_MAX_BUFFER_SIZE`` in subprocess_cli.py); a single
#: large tool payload — a full chapter ``write``, or a research sub-agent returning
#: big context — can exceed that, and when it does the SDK's stdin decoder raises
#: "JSON message exceeded maximum buffer size", closes the input stream, and rejects
#: every pending permission request ("Tool permission stream closed…") → the run dies
#: mid-turn with the "Error in hook callback / Stream closed" cascade. Raising the
#: ceiling to 32 MB gives ample headroom for realistic agent payloads. (Distinct from
#: the durable-defer hook-timeout crash fixed in bf1e4527 — same symptom, other cause.)
_MAX_CONTROL_MESSAGE_BYTES = 32 * 1024 * 1024

logger = logging.getLogger(__name__)


def _bare_custom_tool_name(fq_name: str) -> str:
    """``mcp__harness_custom__web-search`` → ``web-search``.

    ``rsplit("__", 1)`` (not ``split``) because a custom-tool name may itself
    contain ``__``-free words with hyphens; only the LAST ``__`` separates the
    server prefix from the bare tool name.
    """
    return fq_name.rsplit("__", 1)[-1]


def _permission_result_to_hook_decision(
    result: PermissionResultAllow | PermissionResultDeny,
) -> dict:
    """Translate a ``can_use_tool`` result into a ``PreToolUse`` decision dict.

    Deny → the SDK deny shape (its ``permissionDecisionReason`` feeds the model
    so it re-plans); Allow → ``{}`` (fall through — the tool runs). Verified
    against the installed ``claude_agent_sdk`` ``PreToolUseHookSpecificOutput``
    (fields ``hookEventName`` / ``permissionDecision`` / ``permissionDecisionReason``).
    """
    if isinstance(result, PermissionResultDeny):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result.message
                or "Denied by permission handler",
            }
        }
    return {}


class ClaudeSession(BaseProvider):
    """Wraps ClaudeSDKClient for a single workspace session.

    Reference oracle for the widened provider contract (Phase 1): green on
    C1–C5 with per-run auth isolation (B8), pinned session home (C4), custom
    tools (G1/B2), and the kwargs-reject guardrail.
    """

    #: Stable provider key — the durable identity the resume path matches on
    #: (not the class name, which drifts; see bug 4a / N9).
    PROVIDER = "claude"

    # --- Capability flags (design-coverage §4) -------------------------------
    crash_isolated = True  # SDK spawns a CLI child; a crash is contained
    hard_kill_tier = "cooperative"  # SDK owns child PID → interrupt(), not SIGKILL
    cost_visibility = "mid_turn"  # message_delta cumulative tokens
    compaction = "native"  # auto-compact is the baseline; observe + emit
    supports_hard_deadline = False  # cooperative cancel, not a true kill (PROV-4)
    custom_tool_delivery = "in_proc_list"  # SDK-MCP via create_sdk_mcp_server
    perm_tier = "arg_level"  # T4-proven can_use_tool with args
    retry_owner = "sdk"  # the Anthropic/Claude SDK retries transient errors (C4)
    max_output_tokens = None  # C6: native compaction manages the window (not harness)

    def describe_auth(self) -> dict[str, Any]:
        """C7/AUTH-3: report auth mode + fingerprint (never the key)."""
        from warden.providers.auth import describe_auth
        return describe_auth("claude", self._auth_env)

    @staticmethod
    def _merge_hooks(options: Any, hooks: dict) -> None:
        """Append ``hooks``' matchers onto ``options.hooks`` (or set them)."""
        if options.hooks:
            for event_type, matchers in hooks.items():
                options.hooks[event_type] = options.hooks.get(event_type, []) + matchers
        else:
            options.hooks = hooks

    def install_hooks(self, options: Any) -> None:
        """Merge the config-gated audit + safety hooks into the SDK ``options``.

        The generalized ``install_hooks`` seam (C11 / M5 3a-1): when the threaded
        ``AuditConfig`` is enabled, append the audit matchers. Then (SAFE-6 / M4
        3e-2) when the ``PathHookConfig`` is enabled, append the PreToolUse
        path-enforcement matcher — merged AFTER audit so both PreToolUse hooks
        COEXIST. Each gate is independent; a no-op otherwise. Closures capture
        their config at build time (no env at fire time).
        """
        if self._audit and self._audit.enabled:
            from warden.observability.audit.claude_sdk_hooks import build_audit_hooks

            self._merge_hooks(options, build_audit_hooks(
                run_id=self._audit.run_id,
                log_dir=Path(self._audit.log_dir) if self._audit.log_dir else None,
            ))

        # SAFE-6: a config-gated PreToolUse path-enforcement hook that fires even
        # for auto-allowed reads (Read/Grep/Glob) can_use_tool never sees.
        if self._safety_hooks and self._safety_hooks.enabled:
            from warden.safety.permissions.path_hook import build_path_hook

            self._merge_hooks(options, build_path_hook(self._safety_hooks))

        # pre-07b DURABLE mode: a single PreToolUse hook (all tools) that consults
        # a file-backed FileDeferStore — unresolved → permissionDecision:"defer"
        # (eject to disk, run ends); resolved → allow/deny injected on resume for
        # the SAME tool_use_id (exact-id, no re-generation). In durable mode this
        # hook IS the gate, so the warm custom-tool gate below is SKIPPED.
        durable_on = self._durable_defer is not None and getattr(
            self._durable_defer, "enabled", False
        )
        if durable_on:
            from warden.safety.permissions.durable_defer_hook import (
                build_durable_defer_hook,
            )
            from warden.seams.defer_store import FileDeferStore

            store = FileDeferStore(self._durable_defer.store_root)
            # EXT-G1: hand the durable hook a CHECKER-ONLY decision callable so it
            # only defers a confirm-listed (requires_confirmation) tool — siblings
            # auto-allow (the allow-all-except-one gate). None ⇒ legacy defer-all.
            # ``on_defer`` flips a flag the send() loop watches: the SDK's ``defer``
            # does NOT halt the agent loop, so we interrupt the turn the moment a gated
            # tool defers — else a non-yielding orchestrator streams past the gate to
            # completion before the pause surfaces.
            self._merge_hooks(
                options,
                build_durable_defer_hook(
                    store,
                    permission_check=self._durable_permission_check(),
                    on_defer=self._mark_durable_defer,
                ),
            )

        # PERM-3 (pre-07 / M9): route custom-tool calls through the SAME
        # can_use_tool seam regular tools use. Custom tools live in
        # options.allowed_tools (model-callability), which makes the SDK SHADOW
        # can_use_tool for them — so this scoped PreToolUse hook is their ONLY
        # runtime gate. Installed alongside audit/safety (any deny wins). No-op
        # without custom tools or without a seam to consult. Skipped in durable
        # mode (the durable defer hook above gates custom tools too).
        if self._custom_tools and self._can_use_tool is not None and not durable_on:
            self._merge_hooks(options, self._build_custom_tool_gate())

        # B1 — a config-gated top-level ``Stop`` continuation hook. When enabled it
        # blocks an early ``end_turn`` and re-prompts IN-STREAM (same session) until
        # the named completion tool fires + sets an OUTER ``max_turns`` cap. A no-op
        # when the ContinuationConfig is absent/disabled (tracker is None then).
        if self._continuation_tracker is not None:
            install_continuation_hook(
                self._merge_hooks, options, self._continuation_tracker, self._continuation
            )
            # B1 — the SAME workflows that run the continuation loop (course-authoring /
            # improve) are the ones that dispatch research sub-agents via ``Task``. Force
            # those dispatches SYNCHRONOUS so a backgrounded sub-agent can't end the
            # orchestrator's turn and strand the run (Anthropic #47936/#49150, both
            # "not planned"). read-only has no continuation tracker → this never installs
            # there (it spawns no sub-agents anyway).
            from warden.providers.claude.subagent_sync_hook import (
                build_subagent_sync_hook,
            )

            self._merge_hooks(options, build_subagent_sync_hook())

    def _mark_durable_defer(self, tool_name: str, tool_use_id: str | None) -> None:
        """on_defer callback: a gated tool just deferred → flag the send() loop to
        interrupt the turn. Runs in the SDK hook's context (single-threaded asyncio),
        so a plain attribute set is safe."""
        logger.info("Gate defer recorded for %s (%s) → will interrupt turn", tool_name, tool_use_id)
        self._durable_defer_fired = True

    def _durable_permission_check(self):
        """EXT-G1 — a ``(tool_name, tool_input) -> PermissionDecision`` closure over
        the workflow :class:`PermissionChecker`, so the durable-defer hook only
        defers a ``requires_confirmation`` (confirm-listed) tool. Consults the checker
        ONLY (never the handler — no re-entrancy). ``None`` when no checker is wired,
        which keeps the legacy defer-every-tool behavior. Strips the
        ``mcp__harness_custom__`` prefix so a custom tool's bare name matches the
        manifest's confirm list."""
        checker = self._permission_checker
        if checker is None:
            return None

        def _check(tool_name: str, tool_input: dict):
            bare = (
                _bare_custom_tool_name(tool_name)
                if tool_name.startswith(_CUSTOM_TOOL_PREFIX)
                else tool_name
            )
            return checker.evaluate(bare, tool_input or {})

        return _check

    def _build_custom_tool_gate(self) -> dict[str, list[HookMatcher]]:
        """PERM-3: a ``PreToolUse`` gate that makes a ``can_use_tool`` deny
        actually block a custom tool (parity with regular tools + OpenHarness).

        The callback is a CLOSURE over ``self`` so it can reuse the exact seam
        ``self._can_use_tool`` — it must NOT live in the module-level
        ``build_audit_hooks`` (no ``self`` there). Matcher ``^mcp__harness_custom__``
        scopes it to our custom tools ONLY, so regular tools stay on their own
        (unshadowed) ``can_use_tool`` path — no double-gate. The bare tool name
        is recovered (``rsplit`` — names may contain ``-``) and the seam's
        allow/deny is translated to the SDK decision shape.
        """

        async def _gate(hook_input: Any, tool_use_id: str | None, context: Any) -> dict:
            try:
                tool_name = hook_input.get("tool_name")
                # Belt-and-braces beyond the SDK matcher: only our custom tools.
                if not tool_name or not tool_name.startswith(_CUSTOM_TOOL_PREFIX):
                    return {}
                bare = _bare_custom_tool_name(tool_name)
                tool_input = hook_input.get("tool_input", {}) or {}
                # pre-07b / 3a — the hook's own ``tool_use_id`` arg identifies this
                # custom call (the HookContext does not carry it), so pass it via a
                # context dict the seam extracts (case #2 identification).
                seam_ctx = {"tool_use_id": tool_use_id or hook_input.get("tool_use_id")}
                result = await self._can_use_tool(bare, tool_input, seam_ctx)
                return _permission_result_to_hook_decision(result)
            except Exception:
                # Fail CLOSED (deny) on an internal gate error — this IS the
                # permission gate for custom tools, so an error must NEVER let the
                # tool run (that would silently un-gate it). Logged, not swallowed
                # (LAW 4). Contrast the path-enforcement hook, which is defense in
                # depth and fails open.
                logger.exception(
                    "Custom-tool gate error for %s → fail-closed (deny)",
                    hook_input.get("tool_name") if hasattr(hook_input, "get") else "?",
                )
                return _permission_result_to_hook_decision(
                    PermissionResultDeny(
                        behavior="deny",
                        message="custom-tool permission gate internal error (fail-closed)",
                    )
                )

        return {
            "PreToolUse": [
                HookMatcher(matcher=_CUSTOM_TOOL_PREFIX_RE, hooks=[_gate], timeout=5.0)
            ]
        }

    def __init__(
        self,
        repo_path: Path,
        can_use_tool: Any = None,
        model: str | None = None,
        resume_session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        auth_env: dict[str, str] | None = None,
        claude_config_dir: Path | None = None,
        session_id: str | None = None,
        custom_tools: list[CustomTool] | None = None,
        telemetry: TelemetryConfig | None = None,
        audit: AuditConfig | None = None,
        safety_hooks: PathHookConfig | None = None,
        durable_defer: Any = None,
        permission_checker: Any = None,
        continuation: Any = None,
        **kwargs: Any,
    ):
        self._reject_unknown_kwargs(kwargs)
        # Session ID is None until captured from SDK's first message,
        # or set upfront if resuming a known session.
        self.session_id: str | None = resume_session_id
        self.repo_path = repo_path
        self._can_use_tool = can_use_tool
        self._model = model
        self._resume_session_id = resume_session_id
        self._disallowed_tools = disallowed_tools or []
        self._system_prompt = system_prompt
        # Per-run managed-key isolation (B8). When set, options.env drops any
        # inherited Claude credential and uses only these vars — concurrent runs
        # each carry a distinct key with no os.environ bleed. None => inherit.
        self._auth_env: dict[str, str] | None = auth_env
        # Pin the agent home inside the task folder (C4) so the transcript is
        # self-contained and does not bleed across a shared home. None => home.
        self._claude_config_dir: Path | None = (
            Path(claude_config_dir).resolve() if claude_config_dir else None
        )
        # Optional caller-pinned session id (resume/factory N2 passthrough).
        # resume_session_id takes precedence for the active session identity.
        self._pin_session_id: str | None = session_id
        if session_id and not self.session_id:
            self.session_id = session_id
        # G1/B2 — custom tools delivered as an in-proc SDK-MCP server (TOOL-1:
        # consume, never silently drop). Empty list => none registered.
        self._custom_tools: list[CustomTool] = custom_tools or []
        # M3 4a — TelemetryConfig slice threaded to the OTEL env + Langfuse tracer.
        self._telemetry = telemetry
        # M5 3a-1 — AuditConfig slice: gates install_hooks + closurizes run_id/log_dir.
        self._audit = audit
        # SAFE-6 (M4 3e-2) — PathHookConfig slice: gates the PreToolUse path hook.
        self._safety_hooks = safety_hooks
        # pre-07b durable — DurableDeferConfig slice: gates the native-defer hook.
        self._durable_defer = durable_defer
        # EXT-G1 — the workflow PermissionChecker, so the durable-defer hook can be
        # selective (only defer a confirm-listed tool). None ⇒ legacy defer-all.
        self._permission_checker = permission_checker
        # B1 — ContinuationConfig slice (claude-only top-level Stop hook). Build ONE
        # per-session CompletionTracker here so the Stop-hook closure (reads .fired)
        # and the send() observation (calls .mark) share it. None when disabled.
        self._continuation = continuation
        self._continuation_tracker: Any = make_tracker(continuation)
        # EXT-G1/gate — set True by the durable-defer hook's on_defer callback when a
        # confirm-listed tool defers; the send() loop watches it to interrupt the turn
        # immediately (the SDK's defer doesn't stop the agent loop). Reset per send().
        self._durable_defer_fired: bool = False
        self._client: ClaudeSDKClient | None = None
        self._started = False
        self._started_at: float = 0
        self.jsonl_path: str | None = None

    async def start(self) -> None:
        """Create and connect the SDK client."""
        if self._started:
            raise RuntimeError(f"Session {self.session_id} already started")

        options = ClaudeAgentOptions(
            cwd=str(self.repo_path),
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
            disallowed_tools=self._disallowed_tools,
            # Raise the 1 MB default control-message ceiling so a large tool payload
            # doesn't overflow the SDK stdin decoder and kill the run (see the
            # _MAX_CONTROL_MESSAGE_BYTES rationale).
            max_buffer_size=_MAX_CONTROL_MESSAGE_BYTES,
        )
        if self._system_prompt:
            options.system_prompt = self._system_prompt
        if self._model:
            options.model = self._model
        if self._resume_session_id:
            options.resume = self._resume_session_id
        # v15 OTel telemetry — activate Claude SDK native tracing
        from warden.observability.telemetry import build_claude_otel_env

        options.env = build_claude_otel_env(self._telemetry)
        logger.info("Claude OTel: %s", options.env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "disabled")

        # B8 — per-run auth isolation: strip inherited Claude creds, then inject
        # this run's key (single source of truth = BaseProvider.apply_auth_env).
        # No-op when _auth_env is None (inherits parent env, unchanged behavior).
        options.env = self.apply_auth_env(options.env, "claude", self._auth_env)
        # C4 — pin the session home so transcripts don't bleed across a shared
        # home. None => unchanged (~/.claude).
        if self._claude_config_dir is not None:
            options.env["CLAUDE_CONFIG_DIR"] = str(self._claude_config_dir)

        # v14 audit hooks — env-var activation, via the generalized install_hooks
        # seam (C11) instead of an inline block.
        self.install_hooks(options)

        # G1/B2 — register custom tools as an in-proc SDK-MCP server + allow them.
        self._wire_custom_tools(options)

        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._started = True
        self._started_at = time.time()
        logger.info("ClaudeSession started (resume=%s, cwd=%s)", self._resume_session_id, self.repo_path)

    def _wire_custom_tools(self, options: ClaudeAgentOptions) -> None:
        """Build an SDK-MCP server from ``self._custom_tools`` and register it on
        ``options`` (G1/B2, delivery=in_proc_list).

        Each ``CustomTool`` becomes an SDK ``@tool`` whose async handler wraps the
        (sync-or-async) ``ct.handler``, returning MCP text content. The server is
        merged into ``options.mcp_servers`` and each tool's fully-qualified name
        (``mcp__harness_custom__{name}``) is added to ``options.allowed_tools`` so
        the model may call it. No-op when no custom tools were supplied.
        """
        if not self._custom_tools:
            return

        sdk_tools = [self._build_sdk_tool(ct) for ct in self._custom_tools]
        server = create_sdk_mcp_server(_CUSTOM_MCP_SERVER, tools=sdk_tools)
        options.mcp_servers = {
            **(options.mcp_servers or {}),
            _CUSTOM_MCP_SERVER: server,
        }
        fqmns = [f"mcp__{_CUSTOM_MCP_SERVER}__{ct.name}" for ct in self._custom_tools]
        options.allowed_tools = [*(options.allowed_tools or []), *fqmns]
        # PERM-3 (pre-07): these entries INTENTIONALLY shadow can_use_tool — the
        # PreToolUse gate (install_hooks) is what enforces permission for custom
        # tools now, so the SDK's shadow warning for OUR prefix is expected.
        # Suppress only it (message-scoped) so a genuinely-shadowed regular tool
        # still warns.
        warnings.filterwarnings(
            "ignore",
            category=CanUseToolShadowedWarning,
            message=rf".*{re.escape(_CUSTOM_TOOL_PREFIX)}.*",
        )
        logger.info("Claude registered %d custom tool(s): %s", len(fqmns), fqmns)

    @staticmethod
    def _build_sdk_tool(ct: CustomTool) -> Any:
        """Wrap one ``CustomTool`` as an SDK ``@tool``. Handles sync or async
        ``ct.handler`` and returns MCP text content."""

        @tool(ct.name, ct.description, ct.input_schema)
        async def _handler(args: dict) -> dict:
            res = ct.handler(**args)
            if inspect.isawaitable(res):
                res = await res
            return {"content": [{"type": "text", "text": str(res)}]}

        return _handler

    def discover_jsonl_path(self) -> None:
        """Find the JSONL file by scanning ~/.claude/projects/ for our sessionId."""
        if self.jsonl_path or not self.session_id:
            return
        try:
            # C4 — when the session home is pinned, scan ITS projects dir so we
            # never pick up another concurrent session's transcript from a shared
            # home. None => unchanged (~/.claude).
            base = self._claude_config_dir or (Path.home() / ".claude")
            projects_dir = base / "projects"
            if not projects_dir.exists():
                return
            for jsonl_file in projects_dir.rglob("*.jsonl"):
                try:
                    with open(jsonl_file) as f:
                        first_line = f.readline().strip()
                        if not first_line:
                            continue
                        data = json.loads(first_line)
                        if data.get("sessionId") == self.session_id:
                            self.jsonl_path = str(jsonl_file)
                            logger.info("ClaudeSession %s discovered JSONL at %s", self.session_id, self.jsonl_path)
                            return
                except (json.JSONDecodeError, OSError):
                    continue
        except Exception:
            logger.debug("Could not discover JSONL path for session %s", self.session_id)

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Send a prompt and yield each SDK message from the response."""
        if not self._client or not self._started:
            raise RuntimeError("ClaudeSession not started")

        # Langfuse trace (optional — for LLM analytics + sub-agent nesting)
        from warden.observability.telemetry.claude_langfuse_tracer import ClaudeLangfuseTracer

        tracer = ClaudeLangfuseTracer.create(self.session_id, prompt, self._telemetry)

        # Reset the per-turn gate-defer flag (set by the durable hook's on_defer).
        self._durable_defer_fired = False
        await self._client.query(prompt)

        async for msg in self._client.receive_response():
            # Gate interrupt: a confirm-listed tool just deferred. The SDK's ``defer``
            # does NOT halt the agent loop, so without interrupting, a non-yielding
            # orchestrator keeps streaming (research → … → completion) and the pause
            # only surfaces at turn-drain — after the pipeline already ran. Interrupt
            # now so the run parks on the gate BEFORE any downstream work. The pending
            # defer is already recorded; the Runner pauses on drain, resume re-fires it.
            if self._durable_defer_fired:
                try:
                    await self._client.interrupt()
                except Exception:
                    logger.exception("Error interrupting on gate defer %s", self.session_id)
                break
            # Capture session_id from the SDK's first message
            if not self.session_id:
                sdk_sid = getattr(msg, "session_id", None)
                if sdk_sid:
                    self.session_id = sdk_sid
                    logger.info("Claude captured SDK session_id=%s", sdk_sid)
                    if tracer:
                        tracer.update_session_id(sdk_sid)

            # Delegate all Langfuse observation management to the tracer
            if tracer:
                tracer.handle_message(msg)

            # B1 — mark the completion tracker when the named completion tool call
            # is observed in the stream (product-agnostic: any ToolUseBlock whose
            # .name matches the bare-or-prefixed completion tool). The Stop hook
            # (same session) reads .fired to decide whether to allow the stop.
            if self._continuation_tracker is not None:
                observe_completion(self._continuation_tracker, msg)

            yield msg

        # Finalize Langfuse trace
        if tracer:
            tracer.finalize()

    async def stop(self) -> None:
        """Interrupt the current query."""
        if self._client and self._started:
            try:
                await self._client.interrupt()
            except Exception:
                logger.exception("Error interrupting session %s", self.session_id)

    async def close(self) -> None:
        """Disconnect the client and clean up."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("Error disconnecting session %s", self.session_id)
            finally:
                self._client = None
                self._started = False
                logger.info("ClaudeSession %s closed", self.session_id)
