import glob
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.engine.query_engine import QueryEngine
from openharness.permissions.checker import (
    PermissionChecker,
    PermissionMode,
    PermissionSettings,
)
from openharness.tools import create_default_tool_registry

from warden.providers.openharness.permission_bridge import (
    build_auto_confirm_prompt,
)
from warden.config.models import AuditConfig, ProviderConfig, TelemetryConfig
from warden.providers.base_provider import BaseProvider
from warden.seams.custom_tools import CustomTool

logger = logging.getLogger(__name__)

from warden.observability.telemetry import init_openharness_otel

# Default Ollama endpoint
_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_MODEL = "qwen3:1.7b"

# auth_env keys that carry an OpenAI-compatible API key, most-specific first.
_AUTH_ENV_KEY_NAMES = ("OPENAI_API_KEY", "OPENHARNESS_API_KEY")


def _api_key_from_auth_env(auth_env: dict[str, str] | None) -> str | None:
    """Extract a per-run API key from ``auth_env`` (None if absent/empty)."""
    if not auth_env:
        return None
    for name in _AUTH_ENV_KEY_NAMES:
        if auth_env.get(name):
            return auth_env[name]
    return None


# System prompt for the OpenHarness agent
_SYSTEM_PROMPT = (
    "You are a helpful coding assistant. You have access to tools for "
    "reading files, writing files, running shell commands, and searching "
    "the codebase. Use them to help the user with their request."
)


class OpenHarnessSession(BaseProvider):
    """Wraps OpenHarness QueryEngine as a library-integrated agent session.

    Conforms to the uniform provider contract: accepts the full standard typed
    input set, rejects unknown kwargs, declares its capability flags. Permissions
    are ``arg_level`` (B15 closed — the orchestrator's ``can_use_tool`` runs as a
    ``PRE_TOOL_USE`` hook that sees the REAL ``tool_input``). Custom tools are
    consumed via the OpenHarness ``tool_registry`` (in-proc list). ``auth_env``
    supplies the per-run ``api_key``; a caller-provided session home pins the
    transcript dir into the per-task unit.
    """

    #: Stable provider key the resume path matches on (not the class name;
    #: see bug 4a / N9).
    PROVIDER = "openharness"

    # --- Capability flags (design-coverage §4) -------------------------------
    crash_isolated = False  # truly in-process; shared Ollama daemon
    hard_kill_tier = "none"
    cost_visibility = "terminal"  # post-hoc only; not dollars
    compaction = "harness_driven"
    supports_hard_deadline = False  # wall-clock + num_predict, no true kill
    custom_tool_delivery = "in_proc_list"  # tool_registry (consumed)
    perm_tier = "arg_level"  # can_use_tool runs as PRE_TOOL_USE hook (B15 closed)
    retry_owner = "harness"  # bare Ollama/LiteLLM transport → harness owns backoff (C4)
    # C6 — the harness enforces the output window for this harness_driven provider.
    # One source of truth for both the declared capability and the QueryEngine cap.
    _MAX_OUTPUT_TOKENS = 4096
    max_output_tokens = _MAX_OUTPUT_TOKENS

    def describe_auth(self) -> dict:
        """C7/AUTH-3: local Ollama — no cloud credential to fingerprint."""
        from warden.providers.auth import describe_auth
        return describe_auth("openharness", self._auth_env)

    def __init__(
        self,
        repo_path: Path,
        model: str | None = None,
        provider_profile: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        can_use_tool: Any = None,
        resume_session_id: str | None = None,
        session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        auth_env: dict[str, str] | None = None,
        custom_tools: list[CustomTool] | None = None,
        session_home: str | Path | None = None,
        provider_config: "ProviderConfig | None" = None,
        telemetry: TelemetryConfig | None = None,
        audit: "AuditConfig | None" = None,
        **kwargs: Any,
    ):
        self._reject_unknown_kwargs(kwargs)
        # C7 (M8): injected ProviderConfig slice, else the typed config surface.
        from warden.config import get_harness_config
        pc = provider_config or get_harness_config().provider
        self.session_id: str | None = resume_session_id
        self.repo_path = Path(repo_path)
        # Resolution order: explicit param -> typed settings (env override) ->
        # default. The env override lets a container point OpenHarness at the
        # host's Ollama (e.g. host.docker.internal:11434) without code changes
        # at the call site. HarnessSettings' openharness_* fields already carry
        # the same defaults (_DEFAULT_MODEL / _DEFAULT_BASE_URL / "ollama"), so
        # the trailing ``or _DEFAULT`` only matters if a field were blanked.
        self._model = model or pc.openharness_model or _DEFAULT_MODEL
        self._provider_profile = provider_profile or "ollama"
        self._base_url = (
            base_url or pc.openharness_base_url or _DEFAULT_BASE_URL
        )
        # Per-run auth. A managed key from ``auth_env`` becomes THIS run's
        # credential (passed straight to OpenAICompatibleClient — OpenHarness has
        # no cloud env var, so the key is in-process, not env). Resolution order:
        # explicit api_key -> auth_env key -> typed settings -> "ollama" dummy
        # (local Ollama accepts any non-empty key).
        self._auth_env: dict[str, str] | None = auth_env
        self._telemetry = telemetry  # M3 4a — threaded to OTEL init + tracers
        self._audit = audit  # M5 3a-1 — threaded through factory; gating is a later rung
        self._api_key = (
            api_key
            or _api_key_from_auth_env(auth_env)
            or pc.openharness_api_key
            or "ollama"
        )
        self._can_use_tool = can_use_tool
        self._resume_session_id = resume_session_id
        self._system_prompt = system_prompt
        # session_id pins the id if resume_session_id is absent. session_home
        # pins the transcript dir into the per-task unit (None => global
        # ~/.openharness). disallowed_tools accepted for contract uniformity.
        if session_id and not self.session_id:
            self.session_id = session_id
        self._session_home: Path | None = (
            Path(session_home).resolve() if session_home else None
        )
        self._disallowed_tools = disallowed_tools or []
        self._custom_tools: list[CustomTool] = custom_tools or []
        self._engine: QueryEngine | None = None
        self._started = False
        self.jsonl_path: str | None = None
        self._transcript_path: Path | None = None

    async def start(self) -> None:
        """Initialize the OpenHarness engine with Ollama-compatible API client."""
        if self._started:
            raise RuntimeError(f"OpenHarnessSession {self.session_id} already started")

        # v15 OTel — instrument OpenAI-compatible client before use
        init_openharness_otel(self._telemetry)

        # Check Ollama connectivity
        await self._check_ollama_health()

        # Build the OpenAI-compatible client (Ollama exposes OpenAI-compatible API)
        # Ollama's OpenAI-compatible endpoint is at /v1
        ollama_base = self._base_url.rstrip("/")
        if not ollama_base.endswith("/v1"):
            ollama_base = f"{ollama_base}/v1"

        api_client = OpenAICompatibleClient(
            api_key=self._api_key,
            base_url=ollama_base,
        )

        # Build tool registry with all default tools, then register any
        # caller-supplied custom tools (G1/B2, delivery=in_proc_list).
        tool_registry = create_default_tool_registry()
        if self._custom_tools:
            from warden.providers.openharness.custom_tool_adapter import (
                register_custom_tools,
            )

            register_custom_tools(tool_registry, self._custom_tools)
            logger.info(
                "OpenHarness registered %d custom tool(s): %s",
                len(self._custom_tools),
                [ct.name for ct in self._custom_tools],
            )

        # --- Tool permission enforcement -----------------------------------
        # SECURITY: the orchestrator's ``can_use_tool`` callback (workflow-YAML
        # rules + tool scope + user confirmation) is the real permission policy.
        # We enforce it in DEFAULT mode and add the orchestrator's ARG-LEVEL
        # decision as a ``PRE_TOOL_USE`` hook (B15): ``_execute_tool_call`` fires
        # the hook FIRST with the FULL ``{tool_name, tool_input}`` and blocks the
        # tool BEFORE it runs when the hook reports ``blocked`` (fail-closed on
        # deny/unknown/exception). This is the SINGLE arg-level gate.
        #
        # The upstream ``PermissionChecker.evaluate()`` runs AFTER the hook and
        # stays the first line for its OWN hard denies (sensitive-path patterns,
        # explicit deny rules, command deny patterns → allowed=False). But in
        # DEFAULT mode it also demands CONFIRMATION for every ordinary mutating
        # tool; with no ``permission_prompt`` that becomes an unconditional block,
        # starving the model. So we wire an AUTO-CONFIRM prompt: the arg-aware
        # decision already happened in the hook, so the prompt just satisfies the
        # upstream ceremony (no double-deny — a denied tool was already blocked by
        # the hook and never reaches the checker).
        #
        # If no callback was supplied (e.g. standalone/library use with no
        # orchestrator), there is no policy to enforce, so we fall back to
        # FULL_AUTO and log a loud warning that OpenHarness runs unrestricted.
        if self._can_use_tool is not None:
            permission_settings = PermissionSettings(mode=PermissionMode.DEFAULT)
            permission_checker = PermissionChecker(permission_settings)
            permission_prompt = build_auto_confirm_prompt()
        else:
            logger.warning(
                "OpenHarnessSession %s started WITHOUT a can_use_tool callback — "
                "running FULL_AUTO with NO tool-permission enforcement. Workflow "
                "permission rules are NOT applied for this session.",
                self.session_id,
            )
            permission_settings = PermissionSettings(mode=PermissionMode.FULL_AUTO)
            permission_checker = PermissionChecker(permission_settings)
            permission_prompt = None

        # Build the permission-gate + (config-gated) audit hook executor. Audit
        # is gated on the threaded AuditConfig.enabled — not os.environ — and the
        # helper derives AUDIT_RUN_ID / AUDIT_LOG_DIR into the subprocess env.
        from warden.providers.openharness.hook_setup import (
            build_openharness_hook_executor,
        )

        hook_executor = build_openharness_hook_executor(
            can_use_tool=self._can_use_tool,
            audit=self._audit,
            repo_path=self.repo_path,
            api_client=api_client,
            model=self._model,
        )

        # Create the query engine
        self._engine = QueryEngine(
            api_client=api_client,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            cwd=self.repo_path,
            model=self._model,
            system_prompt=self._system_prompt or _SYSTEM_PROMPT,
            max_tokens=self._MAX_OUTPUT_TOKENS,  # C6: shared source of truth
            max_turns=8,
            permission_prompt=permission_prompt,
            hook_executor=hook_executor,
        )

        # On resume, seed the engine with prior conversation from the persisted
        # transcript so the model regains memory of earlier turns (contract S4:
        # resume = re-attach + MEMORY; bug B-OH). A fresh (non-resume) session
        # has no transcript yet, so this is a no-op there.
        if self._resume_session_id:
            self._load_history()

        self._started = True
        logger.info(
            "OpenHarnessSession started (model=%s, profile=%s, cwd=%s)",
            self._model,
            self._provider_profile,
            self.repo_path,
        )

    def _load_history(self) -> None:
        """Seed the engine's history from the persisted transcript on resume.

        A fresh ``QueryEngine`` starts with an EMPTY conversation, so without
        this a resumed session has no memory of earlier turns (bug B-OH) and
        fails contract S4. We parse the transcript
        (``<session_root>/sessions/<sid>.jsonl`` — one JSON object per line
        ``{"type":"user"|"assistant","text",...}``; see ``_write_transcript``)
        into ``ConversationMessage``s and REPLACE the engine's history via
        ``load_messages`` (the same objects ``.messages`` reads back).

        Fail-soft (LAW 4: log, never silently swallow): a missing transcript
        starts cold; a corrupt line is skipped with a warning; an unreadable
        file logs and returns rather than crashing the resume.
        """
        from openharness.engine.messages import ConversationMessage, TextBlock

        self._ensure_transcript()
        path = self._transcript_path
        if not path or not Path(path).exists():
            logger.info(
                "OpenHarness resume %s: no transcript at %s — starting cold.",
                self.session_id, path,
            )
            return

        prior: list = []
        try:
            with open(path) as f:
                for lineno, raw in enumerate(f, 1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "OpenHarness resume %s: skipping corrupt transcript "
                            "line %d.", self.session_id, lineno,
                        )
                        continue
                    role = entry.get("type")
                    text = entry.get("text", "")
                    if role not in ("user", "assistant") or not text:
                        continue
                    prior.append(
                        ConversationMessage(
                            role=role, content=[TextBlock(text=text)]
                        )
                    )
        except OSError as exc:
            logger.warning(
                "OpenHarness resume %s: could not read transcript %s (%s) — "
                "starting cold.", self.session_id, path, exc,
            )
            return

        if prior:
            self._engine.load_messages(prior)
            logger.info(
                "OpenHarness resume %s: seeded %d prior message(s) from transcript.",
                self.session_id, len(prior),
            )

    async def _check_ollama_health(self) -> None:
        """Verify Ollama is reachable by hitting /api/tags."""
        import aiohttp

        url = f"{self._base_url.rstrip('/')}/api/tags"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(
                            f"Ollama returned status {resp.status} at {url}. "
                            f"Is Ollama running? Start it with: ollama serve"
                        )
        except aiohttp.ClientError as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self._base_url}. "
                f"Is Ollama running? Start it with: ollama serve\n"
                f"Error: {e}"
            ) from e

    def _session_root(self) -> Path:
        """The OpenHarness state root — pinned per-task when a session_home was
        provided, else the global ``~/.openharness``."""
        return self._session_home or (Path.home() / ".openharness")

    def _ensure_transcript(self) -> None:
        """Create the JSONL transcript directory and file if needed."""
        if self._transcript_path or not self.session_id:
            return
        transcript_dir = self._session_root() / "sessions"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        self._transcript_path = transcript_dir / f"{self.session_id}.jsonl"

    def _write_transcript(self, entry: dict) -> None:
        """Append a single JSON line to the session transcript."""
        if not self._transcript_path:
            return
        try:
            with open(self._transcript_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.debug("Failed to write transcript for %s", self.session_id)

    async def send(self, prompt: str) -> AsyncGenerator[Any, None]:
        """Send prompt through the OpenHarness engine, yielding StreamEvents."""
        if not self._engine or not self._started:
            raise RuntimeError("OpenHarnessSession not started")

        try:
            # Capture session_id on very first call if not set
            if not self.session_id:
                self.session_id = str(uuid.uuid4())
                logger.info(
                    "OpenHarnessSession captured session_id=%s",
                    self.session_id,
                )

            self._ensure_transcript()

            # Enriched telemetry (optional): Langfuse analytics + OTEL turn/tool
            # spans, driven behind one facade (each leg self-gates internally).
            from warden.observability.telemetry.openharness_tracers import OpenHarnessTracers

            tracers = OpenHarnessTracers.create(
                self.session_id, prompt, self._model, self._telemetry,
            )

            # Record user message
            self._write_transcript({
                "type": "user",
                "sessionId": self.session_id,
                "timestamp": time.time(),
                "text": prompt,
            })

            # Collect assistant text for transcript
            assistant_text = ""

            async for event in self._engine.submit_message(prompt):
                event_type = type(event).__name__

                if event_type == "AssistantTextDelta":
                    assistant_text += getattr(event, "text", "")

                elif event_type == "AssistantTurnComplete":
                    # Write transcript
                    self._write_transcript({
                        "type": "assistant",
                        "sessionId": self.session_id,
                        "timestamp": time.time(),
                        "text": assistant_text,
                    })
                    # Set output before handling turn complete
                    tracers.set_final_output(assistant_text)
                    tracers.handle_event(event)
                    assistant_text = ""
                    if not self.jsonl_path and self._transcript_path:
                        self.jsonl_path = str(self._transcript_path)

                else:
                    # Tool events and others — delegate to the tracers
                    tracers.handle_event(event)

                yield event

            # Finalize enriched telemetry
            tracers.finalize()
        except Exception:
            logger.exception(
                "OpenHarnessSession %s error during send", self.session_id
            )
            raise

    async def stop(self) -> None:
        """Interrupt the current query (best-effort)."""
        # QueryEngine doesn't expose an interrupt API — the generator
        # will be abandoned when the WebSocket handler cancels the task
        pass

    async def close(self) -> None:
        """Shut down the engine and clean up."""
        self._engine = None
        self._started = False
        logger.info("OpenHarnessSession %s closed", self.session_id)

    def discover_jsonl_path(self) -> None:
        """Scan OpenHarness state directory for session transcript."""
        if self.jsonl_path or not self.session_id:
            return
        try:
            oh_dir = self._session_root() / "sessions"
            if not oh_dir.exists():
                return
            pattern = str(oh_dir / "**" / f"*{self.session_id}*.jsonl")
            matches = glob.glob(pattern, recursive=True)
            if matches:
                self.jsonl_path = matches[0]
                logger.info(
                    "OpenHarnessSession %s found JSONL at %s",
                    self.session_id,
                    self.jsonl_path,
                )
        except Exception:
            logger.debug(
                "Could not discover JSONL path for OpenHarnessSession %s",
                self.session_id,
            )
