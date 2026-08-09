#!/usr/bin/env python3
"""Safety Experiment CLI — interactive REPL for exercising orchestration safety layers.

Usage (from the repo root):
    PYTHONPATH=. server/.venv/bin/python -m warden.drive.cli
    PYTHONPATH=. server/.venv/bin/python -m warden.drive.cli --experiment ask-only
    PYTHONPATH=. server/.venv/bin/python -m warden.drive.cli --single "What is 2+2?"

Drives ChatAPI directly — no web server required. Subscription auth works.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from warden import (
    ChatAPI,
    CustomTool,
    ErrorEvent,
    MessageEvent,
    Middleware,
    SessionCreatedEvent,
    ToolAccessNotificationEvent,
)
from warden.config import get_harness_config
from warden.persistence.keys import task_dir
from warden.safety.experiments.presets import EXPERIMENT_PRESETS
from warden.safety.middleware.input.canary import check_canary
from warden.safety.middleware.input.intent import (
    FuzzyIntentClassifier,
    IntentClassifierMiddleware,
)
from warden.safety.middleware.output.filters import (
    StreamingOutputFilter,
    check_output_for_leaks,
)
from warden.safety.middleware.output.sanitize import sanitize_output


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safety Experiment CLI — exercise orchestration safety layers",
    )
    parser.add_argument(
        "prompt", nargs="*", default=[], help="Prompt text (used with --single)",
    )
    parser.add_argument(
        "--provider", choices=["claude", "claude-cli", "openharness", "codex"],
        default="claude", help="LLM provider (default: claude)",
    )
    parser.add_argument("--model", default=None, help="Model override")
    parser.add_argument("--system-prompt", default=None, help="System prompt")
    parser.add_argument(
        "--allowed-tools", default=None,
        help="Comma-separated list of allowed tools",
    )
    parser.add_argument(
        "--denied-tools", default=None,
        help="Comma-separated list of denied tools",
    )
    parser.add_argument("--workflow", default=None, help="Workflow name")
    parser.add_argument(
        "--user-id", default="default",
        help="Persistence user id (default: default). Used with --task-id.",
    )
    parser.add_argument(
        "--task-id", default=None,
        help="Persistence task id. When set, enables guarded restore/backup; "
             "repo_path is derived as base_dir/user_id/task_id.",
    )
    parser.add_argument(
        "--session-db", default=None,
        help="Shared session index DB path (default: data/sessions.db). Point "
             "multiple processes/containers at the same path to share the index.",
    )
    parser.add_argument(
        "--storage-backend", choices=["local", "s3"], default="local",
        help="Persistence store for task snapshots (default: local). 's3' reads "
             "region/endpoint/creds from env; bucket from --s3-bucket or "
             "AWS_BUCKET_NAME.",
    )
    parser.add_argument(
        "--s3-bucket", default=None,
        help="S3 bucket for --storage-backend s3 (falls back to AWS_BUCKET_NAME).",
    )
    parser.add_argument(
        "--s3-prefix", default="",
        help="Optional key prefix within the S3 bucket.",
    )
    parser.add_argument(
        "--experiment", choices=list(EXPERIMENT_PRESETS.keys()), default=None,
        help="Apply a named experiment preset",
    )
    parser.add_argument(
        "--single", action="store_true",
        help="Single-shot mode (no REPL) — uses positional prompt",
    )
    parser.add_argument(
        "--resume", metavar="SESSION_ID", default=None,
        help="Resume a specific session id (threaded into send). "
             "Takes precedence over --continue.",
    )
    parser.add_argument(
        "--continue", dest="continue_session", action="store_true",
        help="Resume the newest session for --task-id's workspace. "
             "Ignored if --resume is set or --task-id is absent.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show tool_use/tool_result events",
    )
    return parser


def parse_tool_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [t.strip() for t in value.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Build ChatAPI from parsed args
# ---------------------------------------------------------------------------

def build_api(args: argparse.Namespace) -> tuple[ChatAPI, dict]:
    """Build a ChatAPI instance from CLI args + experiment preset.

    Returns (api, experiment_flags).
    """
    system_prompt = args.system_prompt
    allowed_tools = parse_tool_list(args.allowed_tools)
    custom_tools: list[CustomTool] | None = None
    middleware: list[Middleware] | None = None
    flags: dict = {}

    # Experiment preset overrides
    if args.experiment:
        preset = EXPERIMENT_PRESETS[args.experiment]
        if preset["system_prompt"] is not None:
            system_prompt = preset["system_prompt"]
        if preset["allowed_tools"] is not None:
            allowed_tools = preset["allowed_tools"]
        if preset["custom_tools"] is not None:
            custom_tools = preset["custom_tools"]
        if preset["middleware"] is not None:
            middleware = preset["middleware"]
        flags = {k: v for k, v in preset.items() if k.startswith("_")}

    # E9: wire intent classifier middleware
    if flags.get("_intent_classifier"):
        middleware = [IntentClassifierMiddleware()]

    # E12: wire fuzzy intent classifier middleware [EXP:E12]
    if flags.get("_fuzzy_classifier"):
        middleware = [FuzzyIntentClassifier()]

    # Start from the env-derived config (picks up .env / OPENHARNESS_* etc.),
    # then overlay the CLI/preset choices onto the relevant sub-configs.
    config = get_harness_config()
    config.provider.provider = args.provider
    config.provider.model = args.model
    config.safety.system_prompt = system_prompt
    config.permissions.allowed_tools = allowed_tools
    config.permissions.denied_tools = parse_tool_list(args.denied_tools)
    config.custom_tools.tools = custom_tools or []
    config.middleware.input_instances = middleware or []
    # §4a master switch: a preset/experiment that wires input middleware means to
    # run it, so flip the input switch on when any is configured.
    config.middleware.enable_input_middleware = bool(middleware)
    config.persistence.session_db_path = args.session_db
    config.persistence.backend = args.storage_backend
    config.persistence.s3.bucket = args.s3_bucket
    config.persistence.s3.prefix = args.s3_prefix
    # Persistence: when --task-id is set, ChatAPI derives repo_path from
    # base_dir/user_id/task_id (overriding repo_path). Absent → behaves as today.
    if args.task_id:
        config.workspace.user_id = args.user_id
        config.workspace.task_id = args.task_id

    # Workflow is init-bound (SESS-1): fixed for the process, not swapped per turn.
    api = ChatAPI(config, repo_path=".", workflow=args.workflow)
    return api, flags


# ---------------------------------------------------------------------------
# Event display
# ---------------------------------------------------------------------------

def format_event(event: object, verbose: bool) -> str | None:
    """Format an orchestrator event for CLI display. Returns None to suppress."""
    if isinstance(event, MessageEvent):
        if event.kind == "text":
            return event.content.get("text", "")
        if event.kind == "tool_use" and verbose:
            name = event.content.get("name", "?")
            inputs = event.content.get("input", {})
            return f"[tool_use] {name}({inputs})"
        if event.kind == "tool_result" and verbose:
            output = str(event.content.get("output", ""))
            if len(output) > 200:
                output = output[:200] + "..."
            return f"[tool_result] {output}"
        return None

    if isinstance(event, ToolAccessNotificationEvent):
        if event.action == "denied":
            return f"[DENIED] Tool '{event.tool_name}' blocked — {event.reason}"
        if verbose:
            return f"[{event.action}] Tool '{event.tool_name}' — {event.reason}"
        return None

    if isinstance(event, ErrorEvent):
        return f"[ERROR] {event.text}"

    return None


# ---------------------------------------------------------------------------
# Message send + display (with optional output filtering)
# ---------------------------------------------------------------------------

def _print_session_id(session_id: str | None) -> None:
    """Print the resolvable session id so the user can resume later."""
    if session_id:
        print(f"\n[session: {session_id}]  (resume with --resume {session_id})")


async def _collect_and_display(
    api: ChatAPI, prompt: str, verbose: bool,
    workflow: str | None, flags: dict, session_id: str | None = None,
) -> str | None:
    """Send a message and display results, applying output filters if flagged.

    Threads ``session_id`` into ``api.send`` for resume, and returns the session
    id observed on the stream (from ``SessionCreatedEvent``) after printing it.
    """
    use_output_filter = flags.get("_output_filter", False)
    use_sanitize = flags.get("_output_sanitize", False)
    use_streaming_filter = flags.get("_streaming_filter", False)
    use_canary = flags.get("_canary_check", False)
    observed_sid: str | None = session_id

    def _track(event: object) -> None:
        nonlocal observed_sid
        if isinstance(event, SessionCreatedEvent):
            observed_sid = event.session_id

    if use_streaming_filter:
        # [EXP:E11] Rolling-buffer streaming output filter
        sfilter = StreamingOutputFilter(buffer_size=200)
        blocked = False
        async for event in api.send(
            prompt, workflow=workflow, session_id=session_id,
        ):
            _track(event)
            formatted = format_event(event, verbose)
            if formatted is None:
                continue
            if isinstance(event, MessageEvent) and event.kind == "text":
                if blocked:
                    continue
                text_to_yield, is_filtered = sfilter.push(formatted)
                if is_filtered:
                    print(f"\n[FILTERED] {text_to_yield}", end="", flush=True)
                    blocked = True
                elif text_to_yield:
                    print(text_to_yield, end="", flush=True)
            else:
                print(formatted, end="", flush=True)
        if not blocked:
            remaining, is_filtered = sfilter.flush()
            if is_filtered:
                print(f"\n[FILTERED] {remaining}", end="", flush=True)
            else:
                print(remaining, end="", flush=True)
    elif use_canary:
        # [EXP:E15] Canary token detection — buffer and check
        text_chunks: list[str] = []
        other_output: list[str] = []
        async for event in api.send(
            prompt, workflow=workflow, session_id=session_id,
        ):
            _track(event)
            formatted = format_event(event, verbose)
            if formatted is None:
                continue
            if isinstance(event, MessageEvent) and event.kind == "text":
                text_chunks.append(formatted)
            else:
                other_output.append(formatted)
        for line in other_output:
            print(line, end="", flush=True)
        full_text = "".join(text_chunks)
        if check_canary(full_text):
            print("[BLOCKED] System prompt leakage detected")
        else:
            print(full_text, end="")
    elif use_output_filter or use_sanitize:
        text_chunks: list[str] = []
        other_output: list[str] = []
        async for event in api.send(
            prompt, workflow=workflow, session_id=session_id,
        ):
            _track(event)
            formatted = format_event(event, verbose)
            if formatted is None:
                continue
            if isinstance(event, MessageEvent) and event.kind == "text":
                text_chunks.append(formatted)
            else:
                other_output.append(formatted)

        for line in other_output:
            print(line, end="", flush=True)

        full_text = "".join(text_chunks)
        if use_output_filter:
            leak_reason = check_output_for_leaks(full_text)
            if leak_reason:
                print(f"[FILTERED] Response contained internal information ({leak_reason})")
            else:
                print(full_text, end="")
        elif use_sanitize:
            sanitized = sanitize_output(full_text)
            if sanitized:
                print(sanitized)
            else:
                print(full_text, end="")
    else:
        async for event in api.send(
            prompt, workflow=workflow, session_id=session_id,
        ):
            _track(event)
            formatted = format_event(event, verbose)
            if formatted is not None:
                print(formatted, end="", flush=True)
    print()
    _print_session_id(observed_sid)
    return observed_sid


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

async def repl(
    api: ChatAPI, verbose: bool, workflow: str | None,
    flags: dict | None = None, session_id: str | None = None,
) -> None:
    """Interactive REPL loop.

    ``session_id`` (if given, e.g. from --resume/--continue) is threaded into the
    FIRST turn to resume; afterwards the orchestrator keeps _current_session_id so
    later turns continue automatically. We keep tracking the observed id for the
    /session command.
    """
    flags = flags or {}
    current_sid = session_id

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        stripped = user_input.strip()
        if not stripped:
            continue

        if stripped == "/quit":
            print("Bye!")
            break

        if stripped == "/session":
            if current_sid:
                print(f"  session: {current_sid}")
                print(f"  resume with: --resume {current_sid}")
            else:
                print("  session: (none yet — send a message first)")
            continue

        if stripped == "/info":
            print(f"  provider:  {api._provider}")
            print(f"  model:     {api._model or '(default)'}")
            sp = api._system_prompt
            if sp:
                display = (str(sp)[:80] + "...") if len(str(sp)) > 80 else str(sp)
                print(f"  system:    {display}")
            else:
                print("  system:    (none)")
            allowed = api._config.permissions.allowed_tools
            if allowed:
                print(f"  tools:     {', '.join(allowed)}")
            else:
                print("  tools:     (unrestricted)")
            if api._middleware:
                print(f"  middleware: {len(api._middleware)} active")
            if api._custom_tools:
                names = [t.name for t in api._custom_tools]
                print(f"  custom:    {', '.join(names)}")
            continue

        current_sid = await _collect_and_display(
            api, stripped, verbose, workflow, flags, session_id=current_sid,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def resolve_session_id(api: ChatAPI, args: argparse.Namespace) -> str | None:
    """Resolve which session (if any) to resume from --resume/--continue.

    --resume (explicit id) wins. Otherwise --continue with --task-id looks up the
    newest session for that task's workspace. Requires api.init() first. Returns
    None to start fresh.
    """
    if args.resume:
        return args.resume
    if not args.continue_session:
        return None
    if not args.task_id:
        print("[continue] --continue needs --task-id; starting fresh.")
        return None

    base_dir = Path(api._config.workspace.base_dir)
    workspace_path = str(task_dir(base_dir, args.user_id, args.task_id).resolve())
    sessions = await api.list_sessions(workspace_path)
    if not sessions:
        print(f"[continue] No prior session for task '{args.task_id}'; starting fresh.")
        return None
    # list_sessions is ordered by updated_at DESC — newest first.
    sid = sessions[0]["session_id"]
    print(f"[continue] Resuming newest session {sid}")
    return sid


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    api, flags = build_api(args)
    await api.init()

    session_id = await resolve_session_id(api, args)

    experiment_label = args.experiment or "none"
    print(
        f"Safety Experiment CLI | provider={args.provider}"
        f" | experiment={experiment_label}"
    )

    try:
        if args.single:
            prompt = " ".join(args.prompt) if args.prompt else "What files are in this repo?"
            await _collect_and_display(
                api, prompt, args.verbose, args.workflow, flags,
                session_id=session_id,
            )
        else:
            await repl(
                api, args.verbose, args.workflow, flags,
                session_id=session_id,
            )
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
