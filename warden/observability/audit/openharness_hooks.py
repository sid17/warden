"""Build OpenHarness audit hook registry using the native hook system.

Constructs a HookRegistry with command hooks that invoke
`python -m warden.observability.audit.openharness_hook_handler` for each audit-relevant event.
The handler receives the payload via $OPENHARNESS_HOOK_PAYLOAD (set by HookExecutor).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from openharness.hooks.events import HookEvent
from openharness.hooks.executor import HookExecutionContext, HookExecutor
from openharness.hooks.loader import HookRegistry
from openharness.hooks.schemas import CommandHookDefinition

logger = logging.getLogger(__name__)

# Events we capture for audit
_AUDIT_EVENTS = [
    HookEvent.PRE_TOOL_USE,
    HookEvent.POST_TOOL_USE,
    HookEvent.SUBAGENT_STOP,
    HookEvent.STOP,
    HookEvent.NOTIFICATION,
]

# The command hook — HookExecutor sets $OPENHARNESS_HOOK_PAYLOAD automatically.
# Use sys.executable so the subprocess uses the same Python interpreter
# (avoids "python not found" on systems where only python3 exists).
_AUDIT_COMMAND = f"{sys.executable} -m warden.observability.audit.openharness_hook_handler"


def build_openharness_audit_hooks(
    cwd: Path,
    api_client,
    model: str,
) -> HookExecutor:
    """Build a HookExecutor with command hooks for all audit events.

    Args:
        cwd: Working directory for subprocess execution.
        api_client: SupportsStreamingMessages — required by HookExecutionContext.
        model: Default model name — required by HookExecutionContext.

    Returns:
        HookExecutor ready to pass to QueryEngine(hook_executor=...).
    """
    registry = HookRegistry()

    for event in _AUDIT_EVENTS:
        registry.register(
            event,
            CommandHookDefinition(
                command=_AUDIT_COMMAND,
                matcher=None,
                block_on_failure=False,
                timeout_seconds=10,
            ),
        )

    context = HookExecutionContext(
        cwd=cwd,
        api_client=api_client,
        default_model=model,
    )

    executor = HookExecutor(registry, context)
    logger.info("OpenHarness audit hooks registered for %d events", len(_AUDIT_EVENTS))
    return executor
