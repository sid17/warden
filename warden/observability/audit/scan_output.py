#!/usr/bin/env python3
"""Output scanner: detects leaked internals in pipeline-generated files.

Usage:
    PYTHONPATH=. python orchestrator/observability/audit/scan_output.py /tmp/audit-course/
    PYTHONPATH=. python orchestrator/observability/audit/scan_output.py /tmp/audit-course/ --append-to orchestrator/observability/audit/reports/my-workspace/audit-report.md
    PYTHONPATH=. python orchestrator/observability/audit/scan_output.py --list-patterns
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Leak patterns
# ---------------------------------------------------------------------------

@dataclass
class LeakPattern:
    """A pattern that detects leaked internals in output files."""

    name: str
    description: str
    regex: re.Pattern
    severity: str  # high, medium, low


PATTERNS: list[LeakPattern] = [
    # High severity — system prompt / skill content
    LeakPattern(
        name="system_reminder_tag",
        description="System reminder XML tag leaked",
        regex=re.compile(r"<system-reminder>", re.IGNORECASE),
        severity="high",
    ),
    LeakPattern(
        name="deferred_tools_tag",
        description="Available deferred tools XML tag leaked",
        regex=re.compile(r"<available-deferred-tools>", re.IGNORECASE),
        severity="high",
    ),
    LeakPattern(
        name="tool_schema_json",
        description="Tool schema JSON fragment leaked",
        regex=re.compile(r'"parameters"\s*:\s*\{\s*"\$schema"'),
        severity="high",
    ),
    LeakPattern(
        name="skill_path",
        description="Reference to .claude/skills/ path",
        regex=re.compile(r"\.claude/skills/"),
        severity="high",
    ),
    LeakPattern(
        name="agent_definition_path",
        description="Reference to .claude/agents/ path",
        regex=re.compile(r"\.claude/agents/"),
        severity="high",
    ),
    # Medium severity — internal path references
    LeakPattern(
        name="claude_dir_path",
        description="Reference to .claude/ directory",
        regex=re.compile(r"\.claude/(?!settings)"),  # .claude/settings is public
        severity="medium",
    ),
    LeakPattern(
        name="orchestrator_path",
        description="Reference to orchestrator/ internal path",
        regex=re.compile(r"orchestrator/(?:core|middleware|experiments|providers)"),
        severity="medium",
    ),
    LeakPattern(
        name="benchmarks_path",
        description="Reference to benchmarks/ internal path",
        regex=re.compile(r"benchmarks/(?:classifiers|runner|corpus)"),
        severity="medium",
    ),
    LeakPattern(
        name="yaml_frontmatter_skill",
        description="YAML frontmatter with skill/agent metadata",
        regex=re.compile(r"^---\s*\n(?:.*\n)*?(?:skill|agent|hook_event).*\n(?:.*\n)*?---", re.MULTILINE),
        severity="medium",
    ),
    # Low severity — possible agent/skill name mentions
    LeakPattern(
        name="hook_event_name",
        description="Hook event name reference (PreToolUse, SubagentStart, etc.)",
        regex=re.compile(r"\b(?:PreToolUse|PostToolUse|SubagentStart|SubagentStop|PostToolUseFailure)\b"),
        severity="low",
    ),
    LeakPattern(
        name="claude_agent_options",
        description="ClaudeAgentOptions SDK reference",
        regex=re.compile(r"ClaudeAgentOptions"),
        severity="low",
    ),
]

SCANNABLE_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".json", ".yaml", ".yml"}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """A single leak finding in a scanned file."""

    file_path: str
    line_number: int
    pattern_name: str
    matched_text: str  # truncated to 80 chars
    severity: str


def scan_file(file_path: Path) -> list[Finding]:
    """Scan a single file for leak patterns."""
    findings: list[Finding] = []
    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return findings

    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        for pattern in PATTERNS:
            match = pattern.regex.search(line)
            if match:
                snippet = match.group(0)[:80]
                findings.append(Finding(
                    file_path=str(file_path),
                    line_number=i,
                    pattern_name=pattern.name,
                    matched_text=snippet,
                    severity=pattern.severity,
                ))
    return findings


def scan_directory(directory: str) -> tuple[int, list[Finding]]:
    """Scan all scannable files in a directory recursively.

    Returns (files_scanned, findings).
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        print(f"Error: directory not found: {directory}", file=sys.stderr)
        return 0, []

    findings: list[Finding] = []
    files_scanned = 0

    for file_path in sorted(dir_path.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in SCANNABLE_EXTENSIONS:
            continue
        files_scanned += 1
        findings.extend(scan_file(file_path))

    return files_scanned, findings


def format_summary(files_scanned: int, findings: list[Finding]) -> str:
    """Format a human-readable summary."""
    lines: list[str] = []
    files_with_findings = len(set(f.file_path for f in findings))
    by_severity = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

    lines.append(f"Files scanned: {files_scanned}")
    lines.append(f"Files with findings: {files_with_findings}")
    lines.append(f"Total findings: {len(findings)}")
    lines.append(f"  High: {by_severity['high']}")
    lines.append(f"  Medium: {by_severity['medium']}")
    lines.append(f"  Low: {by_severity['low']}")

    if findings:
        lines.append("")
        lines.append("Findings:")
        for f in findings:
            lines.append(
                f"  [{f.severity.upper()}] {f.file_path}:{f.line_number} "
                f"({f.pattern_name}) — {f.matched_text}"
            )

    return "\n".join(lines)


def format_report_section(files_scanned: int, findings: list[Finding]) -> str:
    """Format a markdown section for appending to the audit report."""
    lines: list[str] = []
    files_with_findings = len(set(f.file_path for f in findings))
    by_severity = {"high": 0, "medium": 0, "low": 0}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

    lines.append("")
    lines.append("## Output Scan Findings")
    lines.append("")
    lines.append(f"**Files scanned:** {files_scanned}")
    lines.append(f"**Files with findings:** {files_with_findings}")
    lines.append(f"**Total findings:** {len(findings)} "
                 f"(high: {by_severity['high']}, medium: {by_severity['medium']}, low: {by_severity['low']})")
    lines.append("")

    if not findings:
        lines.append("No leaked internals detected in pipeline output.")
        lines.append("")
        lines.append("**Recommendation:** Output is clean. PreToolUse hooks for content "
                     "sanitization are not required for this pipeline, but should be "
                     "enabled as defense-in-depth for production deployments (see "
                     "`safety-config-guide.md`).")
    else:
        lines.append("| File | Line | Pattern | Snippet | Severity |")
        lines.append("|------|-----:|---------|---------|----------|")
        for f in findings:
            snippet = f.matched_text.replace("|", "\\|")
            lines.append(f"| `{f.file_path}` | {f.line_number} | {f.pattern_name} | `{snippet}` | {f.severity} |")
        lines.append("")
        lines.append("**Recommendations:**")
        if by_severity["high"] > 0:
            lines.append("- **HIGH findings:** Deploy PreToolUse hooks on Write/Edit to block "
                        "content containing system prompt fragments, tool schemas, or skill/agent paths. "
                        "Use the callback pattern from `audit/claude_sdk_hooks.py`.")
        if by_severity["medium"] > 0:
            lines.append("- **MEDIUM findings:** Add internal path patterns to the output "
                        "classifier (v13 cascade regex tier). Monitor but don't block initially.")
        if by_severity["low"] > 0:
            lines.append("- **LOW findings:** Informational. May be legitimate references "
                        "in technical content. Review manually before adding to block lists.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan pipeline output files for leaked internals",
    )
    parser.add_argument("directory", nargs="?", help="Directory to scan")
    parser.add_argument(
        "--list-patterns",
        action="store_true",
        help="List all leak detection patterns",
    )
    parser.add_argument(
        "--append-to",
        default=None,
        help="Append findings section to this markdown file",
    )
    args = parser.parse_args()

    if args.list_patterns:
        for p in PATTERNS:
            print(f"  [{p.severity.upper():6s}] {p.name}: {p.description}")
            print(f"          regex: {p.regex.pattern}")
        return

    if not args.directory:
        parser.error("directory is required (or use --list-patterns)")

    files_scanned, findings = scan_directory(args.directory)
    print(format_summary(files_scanned, findings))

    if args.append_to:
        section = format_report_section(files_scanned, findings)
        report_path = Path(args.append_to)
        if report_path.exists():
            with open(report_path, "a") as f:
                f.write(section)
            print(f"\nAppended Output Scan Findings to {args.append_to}")
        else:
            print(f"\nWarning: {args.append_to} not found — skipping append", file=sys.stderr)


if __name__ == "__main__":
    main()
