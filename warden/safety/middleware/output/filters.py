"""Output filters — leak detection and streaming filter."""

from __future__ import annotations

_SKILL_NAMES = [
    "kickoff", "grilling", "deep-research", "github-research",
    "architecture-extraction", "spec", "dev", "plan-phase",
    "build-phase", "verify", "debug", "code-review", "gen-tests",
    "simplify", "handoff", "project-setup", "security-review",
    "oss-discover", "oss-scan", "oss-prune", "oss-sanitize",
    "oss-navigate", "oss-architecture", "oss-usage", "oss-split-docs",
    "oss-verify", "oss-publish", "docsify-setup", "docsify-sidebar",
    "docsify-serve", "update-config", "keybindings-help", "skillsmp",
    "claude-api", "workflow-feedback",
]

_AGENT_NAMES = ["Explore", "general-purpose", "Plan", "statusline-setup"]

_LEAKED_PHRASES = [
    "available skills", "available agents", "I have access to",
    "SKILL.md", "skill files", "agent types",
]


def check_output_for_leaks(text: str) -> str | None:
    """Check if text contains leaked internal info. Returns reason if leaked, None if safe."""
    lower = text.lower()
    skill_hits = sum(1 for s in _SKILL_NAMES if s in lower)
    agent_hits = sum(1 for a in _AGENT_NAMES if a in text)
    phrase_hits = sum(1 for p in _LEAKED_PHRASES if p.lower() in lower)
    reasons = []
    if skill_hits >= 3:
        matched = [s for s in _SKILL_NAMES if s in lower][:5]
        reasons.append(f"skill names: {', '.join(matched)}...")
    if agent_hits >= 2:
        matched = [a for a in _AGENT_NAMES if a in text]
        reasons.append(f"agent names: {', '.join(matched)}")
    if phrase_hits > 0:
        matched = [p for p in _LEAKED_PHRASES if p.lower() in lower]
        reasons.append(f"phrase: '{matched[0]}'")
    if "---\nname:" in text:
        reasons.append("YAML frontmatter (skill file content)")
    if ".claude/" in text:
        reasons.append(".claude/ path reference")
    if "/Users/" in text:
        reasons.append("absolute user path")
    return "; ".join(reasons) if reasons else None


class StreamingOutputFilter:
    """Rolling-buffer output filter for streaming-compatible leak detection."""

    def __init__(self, buffer_size: int = 200):
        self._buffer = ""
        self._buffer_size = buffer_size

    def push(self, chunk: str) -> tuple[str | None, bool]:
        """Push a chunk. Returns (text_to_yield, is_filtered)."""
        self._buffer += chunk
        if len(self._buffer) < self._buffer_size:
            return None, False
        leak = check_output_for_leaks(self._buffer)
        if leak:
            return f"[FILTERED] {leak}", True
        yield_text = self._buffer[:-self._buffer_size]
        self._buffer = self._buffer[-self._buffer_size:]
        return yield_text, False

    def flush(self) -> tuple[str, bool]:
        """Flush remaining buffer at end of stream."""
        leak = check_output_for_leaks(self._buffer)
        if leak:
            return f"[FILTERED] {leak}", True
        return self._buffer, False
