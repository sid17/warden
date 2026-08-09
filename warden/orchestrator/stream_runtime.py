"""Stateless helpers for the orchestrator's streaming turn.

These are pure/free functions extracted from ``Orchestrator.send_message`` to
keep the core engine focused on lifecycle orchestration. Each helper does one
thing: provider session-home layout, provider message normalization, provider
session-type naming, and image-prompt preparation.
"""

import base64
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable

from warden.providers.claude.message_handler import transform_sdk_message
from warden.providers.claude.cli_message_handler import transform_cli_message
from warden.providers.codex.sdk_message_handler import (
    transform_codex_sdk_message,
)
from warden.providers.openharness.message_handler import (
    transform_openharness_message,
)
from warden.persistence import PersistenceConfig
from warden.workspace import ensure_restored, reinject_credentials, snapshot
from warden.schemas.events import SessionCreatedEvent

logger = logging.getLogger(__name__)


def provider_home_kwargs(provider: str, task_dir: Path) -> dict:
    """Return the per-task session-home kwarg for a provider.

    Pins each provider's transcript INSIDE the task dir so it travels with the
    workspace snapshot (durability / S6 crash-recovery): ``claude``/``claude-cli``
    at ``<task>/.claude-home``, ``codex`` at ``<task>/.codex``, ``openharness``
    at ``<task>/.openharness``. Without this pinning a provider writes to its
    GLOBAL home (e.g. ``~/.openharness``) — outside the snapshot — so a wiped
    task dir loses the transcript and resume comes back with no memory. The auth
    token pass-through is automatic — the provider subprocess inherits
    ``os.environ``.
    """
    if provider in ("claude-cli", "claude"):
        return {"claude_config_dir": task_dir / ".claude-home"}
    if provider == "codex":
        return {"codex_home": task_dir / ".codex"}
    if provider == "openharness":
        return {"session_home": task_dir / ".openharness"}
    return {}


def _session_provider_key(session: Any) -> str:
    """The provider a live session belongs to — its stable ``PROVIDER`` key.

    Every real provider session declares ``PROVIDER`` (``"claude"``,
    ``"codex"``, ``"openharness"``, ``"claude-cli"``). Matching on it rather
    than ``type(session).__name__`` is the N9 hardening: a class rename can no
    longer silently break Step-1 reuse (the stale-map root of bug 4a). Falls
    back to ``"claude"`` for a session that predates the attribute.
    """
    return getattr(session, "PROVIDER", "claude")


def get_message_handler(provider: str) -> Callable:
    """Return the message normalizer for the given provider."""
    if provider == "codex":
        # The SDK session yields already-normalized "kind"-keyed dicts, so this
        # is a passthrough. Routing through the legacy ``transform_codex_message``
        # (keys on absent ``"type"``) dropped every event — bug 4b.
        return transform_codex_sdk_message
    if provider == "openharness":
        return transform_openharness_message
    if provider == "claude-cli":
        return transform_cli_message
    return transform_sdk_message


def prepare_image_prompt(prompt: str, images: list[dict] | None) -> tuple[str, str | None]:
    """Decode base64 images to temp files and append their paths to the prompt.

    Returns ``(prompt, temp_image_dir)``. When no images are supplied the prompt
    is returned unchanged and ``temp_image_dir`` is ``None``. The caller owns
    cleanup of ``temp_image_dir``.
    """
    if not images:
        return prompt, None

    temp_image_dir = tempfile.mkdtemp(prefix="warden-images-")
    paths: list[str] = []
    for idx, img in enumerate(images):
        data_str = img.get("data", "")
        if "," in data_str:
            data_str = data_str.split(",", 1)[1]
        ext = Path(img.get("name", "image.png")).suffix or ".png"
        fpath = Path(temp_image_dir) / f"image_{idx}{ext}"
        fpath.write_bytes(base64.b64decode(data_str))
        paths.append(str(fpath))
    prompt += "\n\n[Images provided at the following paths:]\n" + "\n".join(
        f"{i + 1}. {p}" for i, p in enumerate(paths)
    )
    return prompt, temp_image_dir


async def prepare_persisted_turn(
    persist_cfg: PersistenceConfig,
    persist_backend: Any,
    user_id: str,
    task_id: str,
    provider: str,
) -> tuple[Path, dict]:
    """Guarded restore for a persistence-active turn.

    Rebuilds (or no-ops) the task directory before session use, ensures the
    task cwd exists for session create/resume, re-injects the provider credential
    out-of-band (excluded from the snapshot; see ADR credential-backup-separation),
    and computes the provider's session-home kwargs. Returns
    ``(turn_repo_path, provider_kwargs)``.
    """
    td = await ensure_restored(persist_cfg, persist_backend, user_id, task_id)
    if not td.exists():
        # Fresh task: its cwd must exist for the session create/resume.
        td.mkdir(parents=True, exist_ok=True)
    # A1: re-hydrate the credential into the pinned home AFTER restore, BEFORE the
    # turn. The snapshot never carried it (A2), so this is the only thing that
    # authenticates a persisted codex turn — on turn 1 (bootstrap) and after a
    # wiped-then-restored workspace alike.
    reinject_credentials(provider, td)
    return td, provider_home_kwargs(provider, td)


async def resolve_turn_session(
    *,
    session_manager: Any,
    session_id: str | None,
    current_session_id: str | None,
    provider: str,
    model: str | None,
    can_use_tool: Callable,
    disallowed_tools: list,
    system_prompt: str | None,
    custom_tools: list | None,
    repo_path: Path,
    provider_kwargs: dict | None,
) -> tuple[Any, bool, str | None, SessionCreatedEvent | None]:
    """Resolve the session for this turn via the 3-way lookup, else create.

    Order (behavior-preserving): client ID → orchestrator current → DB resume →
    create. Returns ``(session, is_resumed, current_session_id, resumed_event)``
    where ``resumed_event`` is a ``SessionCreatedEvent(resumed=True)`` the caller
    must yield when a DB resume succeeded (``None`` otherwise) — this preserves
    the original in-stream event ordering without the helper being a generator.
    """
    session = None
    is_resumed = False
    resumed_event: SessionCreatedEvent | None = None

    # Step 1: client's session active with matching provider
    if session_id and session_manager.get(session_id):
        candidate = session_manager.get(session_id)
        if _session_provider_key(candidate) == provider:
            session = candidate
            current_session_id = session_id

    # Step 2: current orchestrator session
    if session is None and current_session_id:
        existing = session_manager.get(current_session_id)
        if existing:
            session = existing

    # Step 3: DB resume
    if session is None and session_id:
        db_entry = await session_manager._index.get(session_id)
        if db_entry:
            resume_provider = db_entry.get("provider", provider)
            if resume_provider != provider:
                db_entry = None
        if db_entry:
            resume_provider = db_entry.get("provider", provider)
            try:
                current_session_id, session = await session_manager.resume(
                    session_id=session_id,
                    repo_path=repo_path,
                    can_use_tool=can_use_tool,
                    provider=resume_provider,
                    model=model,
                    disallowed_tools=disallowed_tools,
                    system_prompt=system_prompt,
                    custom_tools=custom_tools or None,
                    provider_kwargs=provider_kwargs or None,
                )
                is_resumed = True
                resumed_event = SessionCreatedEvent(
                    session_id=current_session_id,
                    resumed=True,
                )
            except Exception:
                logger.exception("Failed to resume session %s", session_id)
                session = None

    # Step 4: create fresh
    if session is None:
        session = await session_manager.create(
            repo_path=repo_path,
            can_use_tool=can_use_tool,
            provider=provider,
            model=model,
            disallowed_tools=disallowed_tools,
            system_prompt=system_prompt,
            custom_tools=custom_tools or None,
            provider_kwargs=provider_kwargs or None,
        )

    return session, is_resumed, current_session_id, resumed_event


def compute_turn_deny_list(deny_baseline: list[str], active_tool_scope: Any) -> list[str]:
    """Merge the baseline deny list with the active ToolScope's denied tools.

    Extracted from ``Orchestrator._send`` (M4 3e-2 line-budget offset): the
    baseline union the scope's ``to_disallowed_tools([])``, deduped + sorted.
    ``active_tool_scope`` None (or with ``denied is None``) => baseline only.
    """
    deny_list = list(deny_baseline)
    if active_tool_scope and active_tool_scope.denied is not None:
        deny_list = sorted(set(deny_list + active_tool_scope.to_disallowed_tools([])))
    return deny_list


def assemble_turn_provider_kwargs(
    base: dict,
    provider: str,
    *,
    auth_env: dict[str, str] | None,
    codex_allow_ungated: bool,
    provider_config: Any = None,
    telemetry: Any = None,
    audit: Any = None,
    safety_hooks: Any = None,
    durable_defer: Any = None,
    permission_checker: Any = None,
    continuation: Any = None,
) -> dict:
    """Thread the per-turn provider kwargs onto ``base`` (from the persist branch).

    Five seams merged in one place: the per-run managed key (``auth_env`` — kept
    outside the persist branch so key isolation holds with persistence off), the
    codex-only ungated-custom-tool opt-in (a ``CodexSdkSession`` ctor param), the
    openharness-only ``provider_config`` slice (C7 — its ctor reads model /
    base_url / api_key from it), the ``telemetry`` slice (M3 4a — the OTEL /
    Langfuse config for the claude + openharness sessions' tracers), and the
    ``audit`` slice (M5 3a-1 / 3b — the AuditConfig for the claude + openharness
    + codex sessions; claude gates its hooks on it, openharness accepts it for a
    later rung, codex derives an event-stream audit tap from it). Each is
    injected only for the provider whose ctor accepts it, so the unknown-kwarg
    guard is never tripped elsewhere (codex takes the audit kwarg but NOT the
    telemetry one). The SAFE-6 ``safety_hooks`` slice (M4 3e-2 — the PathHookConfig
    for the claude PreToolUse path-enforcement hook) is claude-only; openharness
    (path-hook deferred) and codex (no hooks) would trip the unknown-kwarg guard,
    so it is NOT injected there. Non-subprocess providers absorb ``auth_env`` via
    ``**kwargs``.
    """
    out = dict(base)
    if auth_env is not None:
        out["auth_env"] = auth_env
    if provider == "codex" and codex_allow_ungated:
        out["allow_ungated_custom_tools"] = True
    if provider == "openharness" and provider_config is not None:
        out["provider_config"] = provider_config
    if provider in ("claude", "openharness") and telemetry is not None:
        out["telemetry"] = telemetry
    if provider in ("claude", "openharness", "codex") and audit is not None:
        out["audit"] = audit
    if provider == "claude" and safety_hooks is not None:
        out["safety_hooks"] = safety_hooks
    # pre-07b durable — claude-only native-defer hook slice (openharness/codex use
    # the DurableDeferHandler via can_use_tool, not a provider kwarg).
    if provider == "claude" and durable_defer is not None:
        out["durable_defer"] = durable_defer
        # EXT-G1: the checker rides alongside the durable slice (claude-only) so the
        # native-defer hook can be selective (only defer a confirm-listed tool).
        # Injected ONLY in durable mode — the warm path already routes confirmation
        # through can_use_tool, and other providers would trip the unknown-kwarg guard.
        if permission_checker is not None:
            out["permission_checker"] = permission_checker
    # B1 — claude-only top-level Stop continuation hook slice. openharness/codex have
    # no equivalent hook and would trip the unknown-kwarg guard, so claude-only.
    if provider == "claude" and continuation is not None:
        out["continuation"] = continuation
    return out


async def finalize_jsonl_path(session: Any, sid: str | None, index: Any) -> None:
    """Discover the session's JSONL transcript and record it on the index.

    No-op when the session has no transcript yet or exposes no discovery hook;
    an index-update failure is logged, never raised (LAW 4 — a persistence miss
    must not break the turn's terminal event).
    """
    if not getattr(session, "jsonl_path", None) and hasattr(session, "discover_jsonl_path"):
        session.discover_jsonl_path()
    if sid and getattr(session, "jsonl_path", None):
        try:
            await index.update_jsonl_path(sid, session.jsonl_path)
        except Exception:
            logger.debug("Failed to update jsonl_path for %s", sid)


async def snapshot_turn(
    persist_cfg: PersistenceConfig,
    persist_backend: Any,
    user_id: str,
    task_id: str,
) -> str | None:
    """Snapshot the task workspace after a turn's files are written.

    Returns ``None`` on success, or a user-facing error message when the
    snapshot fails (LAW 4: never swallow silently — logged here, surfaced by
    the caller as an error event).
    """
    try:
        await snapshot(persist_cfg, persist_backend, user_id, task_id)
        return None
    except Exception:
        logger.exception("Snapshot failed for %s/%s", user_id, task_id)
        return "Failed to persist workspace snapshot"
