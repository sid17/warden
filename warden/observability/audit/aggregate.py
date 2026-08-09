#!/usr/bin/env python3
"""Aggregation script: reads JSONL audit logs, produces structured audit report.

Usage:
    PYTHONPATH=. python warden/observability/audit/aggregate.py warden/observability/audit/logs/
    PYTHONPATH=. python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --dry-run
    PYTHONPATH=. python warden/observability/audit/aggregate.py warden/observability/audit/logs/ --output warden/observability/audit/reports/my-workspace/audit-report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_AGENT = "root"

# Commands touching these patterns are flagged as sensitive
SENSITIVE_PATTERNS = [".env", "credentials", "secret", "token", "curl ", "wget "]


def load_events(logs_dir: str) -> list[dict]:
    """Load all JSONL events from a directory. Skip malformed lines."""
    events: list[dict] = []
    log_path = Path(logs_dir)
    if not log_path.exists():
        print(f"Error: directory not found: {logs_dir}", file=sys.stderr)
        return events

    for jsonl_file in sorted(log_path.glob("*.jsonl")):
        with open(jsonl_file) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    print(
                        f"Warning: malformed JSON at {jsonl_file.name}:{line_num}",
                        file=sys.stderr,
                    )
    return events


def group_events(events: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """Group events by run_id, then by agent_id."""
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for e in events:
        run_id = e.get("run_id", "unknown")
        agent_id = e.get("agent_id") or ROOT_AGENT
        grouped[run_id][agent_id].append(e)
    return grouped


def build_tool_matrix(events: list[dict]) -> dict[str, dict[str, int]]:
    """Build per-agent tool usage counts."""
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.get("event_type") in ("PreToolUse",):
            agent_id = e.get("agent_id") or ROOT_AGENT
            tool_name = e.get("tool_name", "unknown")
            matrix[agent_id][tool_name] += 1
    return dict(matrix)


def build_path_map(events: list[dict]) -> dict[str, dict[str, set[str]]]:
    """Build per-agent read/write path sets."""
    path_map: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"read": set(), "write": set()}
    )
    for e in events:
        if e.get("event_type") != "PreToolUse":
            continue
        tool = e.get("tool_name", "")
        summary = e.get("tool_input_summary", {})
        file_path = summary.get("file_path", "")
        if not file_path:
            continue
        agent_id = e.get("agent_id") or ROOT_AGENT
        if tool == "Read":
            path_map[agent_id]["read"].add(file_path)
        elif tool in ("Write", "Edit", "MultiEdit"):
            path_map[agent_id]["write"].add(file_path)
    return dict(path_map)


def build_command_inventory(events: list[dict]) -> dict[str, list[str]]:
    """Build per-agent command list from Bash tool events."""
    commands: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for e in events:
        if e.get("event_type") != "PreToolUse" or e.get("tool_name") != "Bash":
            continue
        cmd = e.get("tool_input_summary", {}).get("command", "")
        if not cmd:
            continue
        agent_id = e.get("agent_id") or ROOT_AGENT
        if cmd not in seen[agent_id]:
            seen[agent_id].add(cmd)
            commands[agent_id].append(cmd)
    return dict(commands)


def flag_sensitive_commands(commands: dict[str, list[str]]) -> list[str]:
    """Return list of flagged commands."""
    flagged = []
    for agent_id, cmds in commands.items():
        for cmd in cmds:
            cmd_lower = cmd.lower()
            for pattern in SENSITIVE_PATTERNS:
                if pattern in cmd_lower:
                    flagged.append(f"[{agent_id}] `{cmd}` (matches: {pattern})")
                    break
    return flagged


def compute_convergence(
    grouped: dict[str, dict[str, list[dict]]],
) -> dict[str, dict[str, Any]]:
    """Compute per-agent tool stability across runs."""
    # per-agent, per-run tool sets
    agent_run_tools: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for run_id, agents in grouped.items():
        for agent_id, events in agents.items():
            for e in events:
                if e.get("event_type") == "PreToolUse":
                    tool = e.get("tool_name", "")
                    if tool:
                        agent_run_tools[agent_id][run_id].add(tool)

    convergence: dict[str, dict[str, Any]] = {}
    for agent_id, run_tools in agent_run_tools.items():
        all_tools: set[str] = set()
        for tools in run_tools.values():
            all_tools |= tools
        if not all_tools or len(run_tools) < 2:
            convergence[agent_id] = {
                "total_tools": len(all_tools),
                "stable_tools": len(all_tools),
                "stability": 1.0,
                "runs": len(run_tools),
                "note": "single run — convergence not measurable",
            }
            continue
        # Stable = used in every run
        stable = set.intersection(*run_tools.values())
        convergence[agent_id] = {
            "total_tools": len(all_tools),
            "stable_tools": len(stable),
            "stability": len(stable) / len(all_tools) if all_tools else 1.0,
            "runs": len(run_tools),
            "stable_set": sorted(stable),
            "variable_set": sorted(all_tools - stable),
        }
    return convergence


def _group_paths(paths: set[str]) -> list[str]:
    """Group paths by common prefix for display."""
    if not paths:
        return []
    return sorted(paths)


def generate_report(
    events: list[dict],
    grouped: dict[str, dict[str, list[dict]]],
    tool_matrix: dict[str, dict[str, int]],
    path_map: dict[str, dict[str, set[str]]],
    commands: dict[str, list[str]],
    convergence: dict[str, dict[str, Any]],
) -> str:
    """Generate the full markdown audit report."""
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_events = len(events)
    run_count = len(grouped)

    # Header
    lines.append("# v14 Safety Audit Report")
    lines.append("")
    lines.append(f"**Generated:** {now}")
    lines.append(f"**Runs analyzed:** {run_count}")
    lines.append(f"**Total events:** {total_events}")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    all_agents = sorted(set(a for agents in grouped.values() for a in agents))
    all_tools = sorted(set(t for m in tool_matrix.values() for t in m))
    lines.append(f"- **Unique agents:** {len(all_agents)} ({', '.join(all_agents)})")
    lines.append(f"- **Unique tools:** {len(all_tools)} ({', '.join(all_tools)})")
    lines.append(f"- **Runs:** {', '.join(sorted(grouped.keys()))}")
    lines.append("")

    # Tool Usage Matrix
    lines.append("## Tool Usage Matrix")
    lines.append("")
    if tool_matrix:
        header = "| Agent | " + " | ".join(all_tools) + " | Total |"
        sep = "|-------|" + "|".join("------:" for _ in all_tools) + "|------:|"
        lines.append(header)
        lines.append(sep)
        tool_totals: dict[str, int] = defaultdict(int)
        sorted_agents = sorted(
            tool_matrix.keys(),
            key=lambda a: sum(tool_matrix[a].values()),
            reverse=True,
        )
        for agent_id in sorted_agents:
            counts = tool_matrix[agent_id]
            total = sum(counts.values())
            cells = [str(counts.get(t, 0)) for t in all_tools]
            lines.append(f"| {agent_id} | " + " | ".join(cells) + f" | {total} |")
            for t, c in counts.items():
                tool_totals[t] += c
        total_all = sum(tool_totals.values())
        totals_row = [str(tool_totals.get(t, 0)) for t in all_tools]
        lines.append(
            "| **Total** | " + " | ".join(totals_row) + f" | **{total_all}** |"
        )
    lines.append("")

    # Path Access Map
    lines.append("## Path Access Map")
    lines.append("")
    for agent_id in sorted(path_map.keys()):
        access = path_map[agent_id]
        lines.append(f"### {agent_id}")
        lines.append("")
        if access["read"]:
            lines.append("**Reads:**")
            for p in _group_paths(access["read"]):
                lines.append(f"- `{p}`")
        if access["write"]:
            lines.append("**Writes:**")
            for p in _group_paths(access["write"]):
                lines.append(f"- `{p}`")
        lines.append("")

    # Command Inventory
    lines.append("## Command Inventory")
    lines.append("")
    for agent_id in sorted(commands.keys()):
        cmds = commands[agent_id]
        lines.append(f"### {agent_id}")
        lines.append("")
        for cmd in cmds:
            lines.append(f"- `{cmd}`")
        lines.append("")
    flagged = flag_sensitive_commands(commands)
    if flagged:
        lines.append("**Sensitive commands flagged:**")
        for f in flagged:
            lines.append(f"- {f}")
        lines.append("")
    else:
        lines.append("No sensitive commands flagged.")
        lines.append("")

    # Convergence Analysis
    lines.append("## Convergence Analysis")
    lines.append("")
    if convergence:
        lines.append("| Agent | Runs | Total Tools | Stable Tools | Stability | Note |")
        lines.append("|-------|-----:|------------:|-------------:|----------:|------|")
        for agent_id in sorted(convergence.keys()):
            c = convergence[agent_id]
            pct = f"{c['stability']:.0%}"
            note = c.get("note", "")
            if not note and c["stability"] < 0.8:
                note = "LOW — tool usage varies significantly"
            lines.append(
                f"| {agent_id} | {c['runs']} | {c['total_tools']} "
                f"| {c['stable_tools']} | {pct} | {note} |"
            )
        lines.append("")
        # Detail variable tools
        for agent_id, c in sorted(convergence.items()):
            variable = c.get("variable_set", [])
            if variable:
                lines.append(
                    f"- **{agent_id}** variable tools: {', '.join(variable)}"
                )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate JSONL audit logs into a structured report",
    )
    parser.add_argument("logs_dir", help="Directory containing *.jsonl audit logs")
    parser.add_argument(
        "--output",
        default="warden/observability/audit/reports/audit-report.md",
        help="Output path for the audit report",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print event counts per run and agent without writing report",
    )
    args = parser.parse_args()

    events = load_events(args.logs_dir)
    if not events:
        print("No events found.", file=sys.stderr)
        sys.exit(1)

    grouped = group_events(events)
    tool_matrix = build_tool_matrix(events)
    path_map = build_path_map(events)
    commands = build_command_inventory(events)
    convergence = compute_convergence(grouped)

    if args.dry_run:
        print(f"Events loaded: {len(events)}")
        print(f"Runs: {len(grouped)}")
        print()
        for run_id in sorted(grouped.keys()):
            agents = grouped[run_id]
            print(f"Run: {run_id}")
            for agent_id in sorted(agents.keys()):
                print(f"  Agent: {agent_id} — {len(agents[agent_id])} events")
        print()
        print("Tool Usage Matrix:")
        all_tools = sorted(set(t for m in tool_matrix.values() for t in m))
        for agent_id in sorted(tool_matrix.keys()):
            counts = tool_matrix[agent_id]
            tools_str = ", ".join(f"{t}={counts.get(t, 0)}" for t in all_tools)
            print(f"  {agent_id}: {tools_str}")
        print()
        print("Path Access Map:")
        for agent_id in sorted(path_map.keys()):
            access = path_map[agent_id]
            print(f"  {agent_id}: read={len(access['read'])}, write={len(access['write'])}")
        print()
        print("Command Inventory:")
        for agent_id in sorted(commands.keys()):
            print(f"  {agent_id}: {len(commands[agent_id])} unique commands")
        print()
        print("Convergence:")
        for agent_id in sorted(convergence.keys()):
            c = convergence[agent_id]
            print(f"  {agent_id}: {c['stability']:.0%} stable ({c['runs']} runs)")
        return

    report = generate_report(
        events, grouped, tool_matrix, path_map, commands, convergence
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report)
    print(f"Report written to {args.output} ({len(report)} bytes)")


if __name__ == "__main__":
    main()
