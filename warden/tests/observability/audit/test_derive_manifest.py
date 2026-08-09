"""Tests for per-sub-agent manifest-diff derivation (M5 3c / AUD-2).

The fixture builds three agents across two runs (r1, r2):
- root (agent_id None -> ROOT_AGENT): Agent + Bash in both runs.
- writer (agent_id="writer"): Read + Write under src/ in BOTH runs -> converged.
- flaky (agent_id="flaky"): Bash in r1, WebSearch in r2 -> not converged.

Event dicts mirror AuditEvent.to_jsonl_dict shape: keys event_type, run_id,
agent_id, tool_name, tool_input_summary (with file_path / command).
"""

from __future__ import annotations

import json

from warden.observability.audit.aggregate import ROOT_AGENT
from warden.observability.audit.derive_manifest import (
    derive_manifests,
    main,
)


# ---------------------------------------------------------------------------
# Fixture: 3 agents x 2 runs
# ---------------------------------------------------------------------------

FIXTURE_EVENTS = [
    # --- run r1 ---
    # root orchestrator (agent_id absent/None)
    {"event_type": "PreToolUse", "run_id": "r1", "agent_id": None, "tool_name": "Agent", "tool_input_summary": {}},
    {"event_type": "PreToolUse", "run_id": "r1", "agent_id": None, "tool_name": "Bash", "tool_input_summary": {"command": "ls"}},
    # writer sub-agent
    {"event_type": "PreToolUse", "run_id": "r1", "agent_id": "writer", "tool_name": "Read", "tool_input_summary": {"file_path": "src/module.py"}},
    {"event_type": "PreToolUse", "run_id": "r1", "agent_id": "writer", "tool_name": "Write", "tool_input_summary": {"file_path": "src/out.py"}},
    # flaky sub-agent — Bash only
    {"event_type": "PreToolUse", "run_id": "r1", "agent_id": "flaky", "tool_name": "Bash", "tool_input_summary": {"command": "echo hi"}},
    # --- run r2 ---
    {"event_type": "PreToolUse", "run_id": "r2", "agent_id": None, "tool_name": "Agent", "tool_input_summary": {}},
    {"event_type": "PreToolUse", "run_id": "r2", "agent_id": None, "tool_name": "Bash", "tool_input_summary": {"command": "ls"}},
    # writer — same tools + paths under src/
    {"event_type": "PreToolUse", "run_id": "r2", "agent_id": "writer", "tool_name": "Read", "tool_input_summary": {"file_path": "src/module.py"}},
    {"event_type": "PreToolUse", "run_id": "r2", "agent_id": "writer", "tool_name": "Write", "tool_input_summary": {"file_path": "src/other.py"}},
    # flaky — WebSearch instead of Bash -> variable
    {"event_type": "PreToolUse", "run_id": "r2", "agent_id": "flaky", "tool_name": "WebSearch", "tool_input_summary": {}},
]


def test_root_kept_broad():
    result = derive_manifests(FIXTURE_EVENTS)
    assert result[ROOT_AGENT]["disallowed_tools"] == []


def test_flaky_not_converged_stays_broad():
    result = derive_manifests(FIXTURE_EVENTS)
    assert result["flaky"]["disallowed_tools"] == []
    assert "converg" in result["flaky"]["status"].lower()


def test_writer_disallowed_is_universe_minus_used():
    result = derive_manifests(FIXTURE_EVENTS)
    disallowed = result["writer"]["disallowed_tools"]
    assert disallowed  # non-empty
    # Active universe = {Agent, Bash, Read, Write, WebSearch}.
    # writer used Read + Write, so those must NOT be disallowed.
    assert "Read" not in disallowed
    assert "Write" not in disallowed
    # Everything else in the universe should be disallowed.
    assert "Agent" in disallowed
    assert "Bash" in disallowed
    assert "WebSearch" in disallowed


def test_writer_globs_from_paths():
    result = derive_manifests(FIXTURE_EVENTS)
    read_globs = result["writer"]["read_globs"]
    write_globs = result["writer"]["write_globs"]
    assert any(g.startswith("src/") and g.endswith("**") for g in read_globs)
    assert any(g.startswith("src/") and g.endswith("**") for g in write_globs)


def test_writer_pretool_path_rule():
    result = derive_manifests(FIXTURE_EVENTS)
    rules = result["writer"]["pretool_path_rules"]
    assert isinstance(rules, list) and rules
    rule = rules[0]
    assert rule["hook"] == "PreToolUse"
    assert rule["allow_path_globs"]


def test_writer_status_locked():
    result = derive_manifests(FIXTURE_EVENTS)
    assert result["writer"]["status"].startswith("locked")


def test_main_writes_output_under_audit_tree(tmp_path, capsys):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    jsonl = logs_dir / "run.jsonl"
    jsonl.write_text("\n".join(json.dumps(e) for e in FIXTURE_EVENTS) + "\n")

    output = tmp_path / "manifest-diff.json"
    main([str(logs_dir), "--output", str(output)])

    assert output.exists()
    data = json.loads(output.read_text())
    assert "writer" in data
    # Default path (documented) must live under the current audit tree,
    # never the stale orchestrator/ tree.
    from warden.observability.audit import derive_manifest as dm

    assert "orchestrator/" not in dm.DEFAULT_OUTPUT
    assert "warden/observability/audit" in dm.DEFAULT_OUTPUT
