"""Build the OpenHarness QueryEngine hook executor (permission gate + audit).

Extracted from OpenHarnessSession.start() (M5 3a-2) — keeps session.py under the
500-line limit and houses the config-first audit gating in one testable place.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_openharness_hook_executor(
    *,
    can_use_tool: Any,
    audit: Any,            # AuditConfig | None
    repo_path: Path,
    api_client: Any,
    model: str,
) -> Any:
    """Return the QueryEngine hook_executor: the permission-gate hook + (if
    ``audit.enabled``) the audit command-hook executor, or ``None`` when neither.

    Audit is CONFIG-gated (``audit.enabled``), not ``os.environ["AUDIT_ENABLED"]``.
    OpenHarness audit hooks are command SUBPROCESSES that read ``AUDIT_RUN_ID`` /
    ``AUDIT_LOG_DIR`` from env at fire time, so we DERIVE those from ``audit`` and
    set them in the process env the subprocess inherits (config -> env only at the
    subprocess boundary). This is process-scoped — the OpenHarness command-hook
    API takes no per-hook env, so per-session env isolation would need an
    OpenHarness-library change (out of scope, same posture as OBS-3 3c).
    """
    from warden.providers.openharness.permission_bridge import (
        PermissionHookExecutor,
        build_permission_hook,
    )

    audit_executor = None
    if audit is not None and audit.enabled:
        # config -> env at the subprocess boundary (derived, not read as input)
        os.environ["AUDIT_RUN_ID"] = audit.run_id
        if audit.log_dir:
            os.environ["AUDIT_LOG_DIR"] = str(audit.log_dir)
        from warden.observability.audit.openharness_hooks import (
            build_openharness_audit_hooks,
        )
        audit_executor = build_openharness_audit_hooks(
            cwd=repo_path, api_client=api_client, model=model,
        )
        logger.info("OpenHarness audit hooks enabled (run_id=%s)", audit.run_id)

    if can_use_tool is not None:
        return PermissionHookExecutor(
            build_permission_hook(can_use_tool),
            audit_executor=audit_executor,
        )
    return audit_executor
