#!/usr/bin/env python3
"""Derive a per-sub-agent workflow-manifest DIFF from audit logs (M5 3c / AUD-2).

Consumes the aggregated per-agent audit structure (reusing aggregate.py's
builders) and PROPOSES least-privilege manifest scoping per sub-agent:
disallowed_tools (tools never used), read/write path globs (Path Access Map),
Bash allow-rules (Command Inventory), and a PreToolUse path rule that composes
with M4's SAFE-6 hook. The root orchestrator is kept broad; agents that have not
converged (<80% tool stability across runs) are left broad — not ready to lock
down. This tool PROPOSES a diff; it never auto-applies it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import PurePosixPath
from typing import Any

from warden.observability.audit.aggregate import (
    ROOT_AGENT,
    build_command_inventory,
    build_path_map,
    build_tool_matrix,
    compute_convergence,
    group_events,
    load_events,
)

DEFAULT_THRESHOLD = 0.8

# Default output path — lives under the CURRENT audit tree, never the stale
# orchestrator/ tree.
DEFAULT_OUTPUT = (
    "warden/observability/audit/reports/manifest-diff.json"
)

# Tools whose PreToolUse path rule M4's SAFE-6 hook enforces.
_PATH_TOOLS = ["Read", "Write", "Edit", "MultiEdit"]


def _dir_globs(paths: set[str]) -> list[str]:
    """Turn concrete file paths into sorted, de-duped directory globs.

    Each path -> its parent dir + "/**". A path with no dir -> "*".
    """
    globs: set[str] = set()
    for p in paths:
        parent = str(PurePosixPath(p).parent)
        if parent in ("", "."):
            globs.add("*")
        else:
            globs.add(f"{parent}/**")
    return sorted(globs)


def _broad_entry(convergence: float, status: str) -> dict[str, Any]:
    """A manifest entry that keeps the agent broad (root / not-converged)."""
    return {
        "disallowed_tools": [],
        "read_globs": [],
        "write_globs": [],
        "bash_rules": {"allow_commands": []},
        "pretool_path_rules": [],
        "convergence": convergence,
        "status": status,
    }


def derive_manifests(
    events: list[dict], threshold: float = DEFAULT_THRESHOLD
) -> dict[str, dict]:
    """Return {agent_id: manifest_diff_entry} from raw audit events.

    Root orchestrator and not-yet-converged agents are kept broad. Converged
    sub-agents get least-privilege scoping (disallowed_tools, path globs,
    bash allow-rules, a PreToolUse path rule).
    """
    grouped = group_events(events)
    tool_matrix = build_tool_matrix(events)
    path_map = build_path_map(events)
    commands = build_command_inventory(events)
    convergence = compute_convergence(grouped)

    # Active tool universe = every tool actually in play across all agents.
    active_universe: set[str] = set()
    for tools in tool_matrix.values():
        active_universe |= set(tools.keys())

    # Every agent that appears anywhere.
    all_agents: set[str] = set(convergence.keys()) | set(tool_matrix.keys())

    result: dict[str, dict] = {}
    for agent_id in all_agents:
        conv = convergence.get(agent_id, {})
        stability = conv.get("stability", 1.0)
        runs = conv.get("runs", 0)
        note = conv.get("note")

        # Root is always kept broad — safety comes from scoping the sub-agents
        # it spawns, not from locking the orchestrator itself.
        if agent_id == ROOT_AGENT:
            result[agent_id] = _broad_entry(
                stability,
                "broad (root orchestrator — always kept broad; scope its "
                "sub-agents instead)",
            )
            continue

        # Not converged: single-run (note present) OR stability below threshold.
        if note is not None:
            result[agent_id] = _broad_entry(
                stability,
                "broad (single run — convergence not measurable; not ready "
                "to lock down)",
            )
            continue
        if stability < threshold:
            result[agent_id] = _broad_entry(
                stability,
                f"broad (not converged: stability={stability:.2f} < "
                f"{threshold:.2f} across {runs} runs — tool usage still "
                f"varying, not ready to lock down)",
            )
            continue

        # Converged sub-agent -> propose least-privilege scoping.
        used_tools = set(tool_matrix.get(agent_id, {}).keys())
        disallowed = sorted(active_universe - used_tools)

        access = path_map.get(agent_id, {"read": set(), "write": set()})
        read_globs = _dir_globs(access.get("read", set()))
        write_globs = _dir_globs(access.get("write", set()))

        allow_commands = list(commands.get(agent_id, []))

        pretool_path_rules: list[dict[str, Any]] = []
        allow_path_globs = sorted(set(read_globs) | set(write_globs))
        if allow_path_globs:
            pretool_path_rules.append(
                {
                    "hook": "PreToolUse",
                    "match_tools": list(_PATH_TOOLS),
                    "allow_path_globs": allow_path_globs,
                    "on_violation": "deny",
                }
            )

        result[agent_id] = {
            "disallowed_tools": disallowed,
            "read_globs": read_globs,
            "write_globs": write_globs,
            "bash_rules": {"allow_commands": allow_commands},
            "pretool_path_rules": pretool_path_rules,
            "convergence": stability,
            "status": f"locked (stability={stability:.2f}, runs={runs})",
        }

    return result


def _summarize(manifests: dict[str, dict]) -> str:
    """Short human-readable summary of the proposed diff."""
    lines = [f"Proposed manifest diff for {len(manifests)} agent(s):"]
    for agent_id in sorted(manifests.keys()):
        entry = manifests[agent_id]
        lines.append(f"  {agent_id}: {entry['status']}")
    lines.append("(PROPOSED — not applied.)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Derive a per-sub-agent workflow-manifest DIFF from audit "
        "logs (PROPOSES, never auto-applies)",
    )
    parser.add_argument("logs_dir", help="Directory containing *.jsonl audit logs")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output path for the manifest-diff JSON",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="Convergence stability threshold to lock down an agent (0-1)",
    )
    args = parser.parse_args(argv)

    events = load_events(args.logs_dir)
    if not events:
        print("No events found.", file=sys.stderr)
        sys.exit(1)

    manifests = derive_manifests(events, threshold=args.threshold)

    output_path = args.output
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(manifests, f, indent=2, sort_keys=True)

    print(_summarize(manifests))
    print(f"\nManifest diff written to {output_path}")


if __name__ == "__main__":
    main()
