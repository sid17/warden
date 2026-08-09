"""Record non-hook terminal actions into the audit JSONL trail (M5 3d / AUD-3).

The governance `stopped(reason)` verdict is PRODUCED by M2 (the Governor) and
FOLDED by the runner — it is not a provider hook event, so it never reaches the
Claude/OpenHarness/Codex audit paths. This helper lets the runner RECORD it as a
terminal Stop in the same per-run JSONL, so a budget/deadline kill is forensically
visible in the trail. Config-gated: a no-op unless audit is enabled.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from warden.observability.audit.claude_sdk_hooks import AuditLogWriter
from warden.schemas.audit import AuditEvent


def write_governance_stop(audit: Any, session_id: str | None, reason: str) -> None:
    """Append a Stop AuditEvent recording a governance halt. No-op if audit off.

    ``audit`` is an ``AuditConfig`` (``enabled``/``run_id``/``log_dir``). Uses the
    AUDIT run_id so the line lands in the same ``{run_id}.jsonl`` as the provider
    hook events. Never raises — recording must not break terminal emission.
    """
    if audit is None or not getattr(audit, "enabled", False):
        return
    try:
        log_dir = Path(audit.log_dir) if audit.log_dir else None
        AuditLogWriter(log_dir).append(AuditEvent(
            event_type="Stop",
            timestamp=datetime.now(timezone.utc).isoformat(),
            run_id=audit.run_id,
            session_id=session_id or "",
            stop_reason=reason,
            gen_ai_operation_name="stop",
        ))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("governance-stop audit record failed")
