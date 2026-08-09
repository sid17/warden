"""M5 3d / AUD-3 — write_governance_stop records a terminal Stop into the JSONL.

The governance verdict is produced by M2 + folded by the runner; this helper
RECORDS it (config-gated). N13 also repoints aggregate.py's stale default path.
"""

from __future__ import annotations

import json
from pathlib import Path

from warden.config.models import AuditConfig
from warden.observability.audit.record import write_governance_stop


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_write_governance_stop_records_terminal_stop(tmp_path):
    cfg = AuditConfig(enabled=True, run_id="run-gov", log_dir=str(tmp_path))
    write_governance_stop(cfg, "sess-1", "budget")

    out = tmp_path / "run-gov.jsonl"
    assert out.exists()
    lines = _read_lines(out)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["event_type"] == "Stop"
    assert rec["stop_reason"] == "budget"
    assert rec["run_id"] == "run-gov"
    assert rec["session_id"] == "sess-1"
    assert rec["gen_ai.operation.name"] == "stop"


def test_write_governance_stop_noop_when_disabled(tmp_path):
    cfg = AuditConfig(enabled=False, run_id="run-gov", log_dir=str(tmp_path))
    write_governance_stop(cfg, "sess-1", "budget")

    assert not (tmp_path / "run-gov.jsonl").exists()
    assert list(tmp_path.iterdir()) == []


def test_aggregate_output_default_repointed():
    """N13: aggregate.py's --output argparse default points at warden/…"""
    import inspect

    from warden.observability.audit import aggregate

    src = inspect.getsource(aggregate.main)
    assert 'default="warden/observability/audit/reports/audit-report.md"' in src
    assert "orchestrator/observability/audit" not in src
