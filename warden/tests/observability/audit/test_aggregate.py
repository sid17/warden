"""Tests for audit aggregation logic."""

from __future__ import annotations

import json


from warden.observability.audit.aggregate import (
    build_command_inventory,
    build_path_map,
    build_tool_matrix,
    compute_convergence,
    flag_sensitive_commands,
    group_events,
    load_events,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_EVENTS = [
    # Run 1 — root agent
    {"event_type": "PreToolUse", "run_id": "run-1", "agent_id": None, "tool_name": "Bash", "tool_input_summary": {"command": "mkdir -p /tmp/out"}},
    {"event_type": "PostToolUse", "run_id": "run-1", "agent_id": None, "tool_name": "Bash", "tool_input_summary": {"command": "mkdir -p /tmp/out"}},
    # Run 1 — agent-a (researcher)
    {"event_type": "SubagentStart", "run_id": "run-1", "agent_id": "agent-a", "agent_type": "general-purpose"},
    {"event_type": "PreToolUse", "run_id": "run-1", "agent_id": "agent-a", "tool_name": "Write", "tool_input_summary": {"file_path": "/tmp/out/research.md"}},
    {"event_type": "PostToolUse", "run_id": "run-1", "agent_id": "agent-a", "tool_name": "Write", "tool_input_summary": {"file_path": "/tmp/out/research.md"}},
    {"event_type": "SubagentStop", "run_id": "run-1", "agent_id": "agent-a"},
    # Run 1 — agent-b (writer)
    {"event_type": "SubagentStart", "run_id": "run-1", "agent_id": "agent-b", "agent_type": "general-purpose"},
    {"event_type": "PreToolUse", "run_id": "run-1", "agent_id": "agent-b", "tool_name": "Read", "tool_input_summary": {"file_path": "/tmp/out/research.md"}},
    {"event_type": "PreToolUse", "run_id": "run-1", "agent_id": "agent-b", "tool_name": "Write", "tool_input_summary": {"file_path": "/tmp/out/module.md"}},
    {"event_type": "SubagentStop", "run_id": "run-1", "agent_id": "agent-b"},
    # Run 2 — root agent (same tools as run 1)
    {"event_type": "PreToolUse", "run_id": "run-2", "agent_id": None, "tool_name": "Bash", "tool_input_summary": {"command": "mkdir -p /tmp/out"}},
    # Run 2 — agent-a (uses Read instead of Write — variable)
    {"event_type": "PreToolUse", "run_id": "run-2", "agent_id": "agent-a", "tool_name": "Read", "tool_input_summary": {"file_path": "/tmp/out/existing.md"}},
    {"event_type": "PreToolUse", "run_id": "run-2", "agent_id": "agent-a", "tool_name": "Write", "tool_input_summary": {"file_path": "/tmp/out/research2.md"}},
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGroupEvents:
    def test_groups_by_run_and_agent(self):
        grouped = group_events(FIXTURE_EVENTS)
        assert "run-1" in grouped
        assert "run-2" in grouped
        assert "root" in grouped["run-1"]
        assert "agent-a" in grouped["run-1"]
        assert "agent-b" in grouped["run-1"]

    def test_none_agent_becomes_root(self):
        grouped = group_events(FIXTURE_EVENTS)
        root_events = grouped["run-1"]["root"]
        assert all(e.get("agent_id") is None for e in root_events)


class TestToolMatrix:
    def test_counts_correct(self):
        matrix = build_tool_matrix(FIXTURE_EVENTS)
        assert matrix["root"]["Bash"] == 2  # run-1 + run-2
        assert matrix["agent-a"]["Write"] == 2  # run-1 + run-2
        assert matrix["agent-b"]["Read"] == 1
        assert matrix["agent-b"]["Write"] == 1

    def test_only_counts_pre_tool_use(self):
        matrix = build_tool_matrix(FIXTURE_EVENTS)
        # PostToolUse should not be counted
        total = sum(sum(v.values()) for v in matrix.values())
        pre_count = sum(1 for e in FIXTURE_EVENTS if e["event_type"] == "PreToolUse")
        assert total == pre_count


class TestPathMap:
    def test_read_write_paths(self):
        pm = build_path_map(FIXTURE_EVENTS)
        assert "/tmp/out/research.md" in pm["agent-a"]["write"]
        assert "/tmp/out/research.md" in pm["agent-b"]["read"]
        assert "/tmp/out/module.md" in pm["agent-b"]["write"]

    def test_no_paths_for_bash(self):
        pm = build_path_map(FIXTURE_EVENTS)
        # Root only uses Bash — no file_path in its summaries
        assert "root" not in pm


class TestCommandInventory:
    def test_deduplicates(self):
        cmds = build_command_inventory(FIXTURE_EVENTS)
        # Same command in run-1 and run-2 — deduped per agent
        assert cmds["root"] == ["mkdir -p /tmp/out"]

    def test_per_agent(self):
        cmds = build_command_inventory(FIXTURE_EVENTS)
        assert "agent-a" not in cmds  # agent-a doesn't use Bash


class TestFlagSensitive:
    def test_flags_env(self):
        cmds = {"root": ["cat .env", "ls"]}
        flagged = flag_sensitive_commands(cmds)
        assert len(flagged) == 1
        assert ".env" in flagged[0]

    def test_no_flags(self):
        cmds = {"root": ["mkdir -p /tmp/out"]}
        assert flag_sensitive_commands(cmds) == []


class TestConvergence:
    def test_single_run_agents(self):
        grouped = group_events(FIXTURE_EVENTS)
        conv = compute_convergence(grouped)
        # agent-b only in run-1
        assert conv["agent-b"]["stability"] == 1.0
        assert "single run" in conv["agent-b"].get("note", "")

    def test_multi_run_agent(self):
        grouped = group_events(FIXTURE_EVENTS)
        conv = compute_convergence(grouped)
        # agent-a: run-1 uses Write, run-2 uses Read+Write
        # stable = Write (in both), variable = Read (only run-2)
        assert conv["agent-a"]["runs"] == 2
        assert conv["agent-a"]["stability"] < 1.0  # Read is variable

    def test_root_stability(self):
        grouped = group_events(FIXTURE_EVENTS)
        conv = compute_convergence(grouped)
        # root uses Bash in both runs
        assert conv["root"]["stability"] == 1.0


class TestLoadEvents:
    def test_loads_valid_jsonl(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        log_file.write_text(
            json.dumps({"event_type": "Stop", "run_id": "r1"}) + "\n"
            + json.dumps({"event_type": "Stop", "run_id": "r1"}) + "\n"
        )
        events = load_events(str(tmp_path))
        assert len(events) == 2

    def test_skips_malformed_lines(self, tmp_path, capsys):
        log_file = tmp_path / "bad.jsonl"
        log_file.write_text(
            '{"event_type": "Stop"}\n'
            'NOT VALID JSON\n'
            '{"event_type": "PreToolUse"}\n'
        )
        events = load_events(str(tmp_path))
        assert len(events) == 2
        captured = capsys.readouterr()
        assert "malformed" in captured.err

    def test_empty_dir(self, tmp_path):
        events = load_events(str(tmp_path))
        assert events == []
