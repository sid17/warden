"""Enhanced regex + pattern output classifier — extends baseline with PII/path/env patterns."""

from __future__ import annotations

import re

from warden.safety.middleware.classifiers import ClassifyResult, measure_latency

# All baseline patterns from regex_output.py
_SKILL_NAMES = [
    "kickoff", "grilling", "deep-research", "architecture-extraction",
    "spec", "dev", "plan-phase", "build-phase", "verify", "debug",
    "code-review", "gen-tests", "simplify", "handoff", "project-setup",
    "security-review",
]

_AGENT_NAMES = ["Explore", "general-purpose", "Plan", "statusline-setup"]

_LEAKED_PHRASES = [
    "available skills", "available agents", "I have access to",
    "SKILL.md", "skill files", "agent types",
]

_SANITIZE_PATTERNS = [
    r"\.claude/",
    r"SKILL\.md",
    r"/Users/\w+/",
    r"---\s*\nname:",
    r"when_to_use:",
    r"domain:\s+\w+",
]

# Extended patterns for PII, paths, and env vars
_PII_PATTERNS = [
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",  # Email
    r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b",  # US phone number
    r"\b\d{3}-\d{2}-\d{4}\b",  # SSN pattern
]

_PATH_PATTERNS = [
    r"/Users/\w+",
    r"/home/\w+",
    r"C:\\Users\\\w+",
    r"~/.ssh/",
    r"~/.aws/",
]

_ENV_PATTERNS = [
    r"export\s+\w*(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)\w*\s*=",
    r"\$\w*(?:SECRET|KEY|TOKEN|PASSWORD)\w*",
]

_CONFIG_FILE_PATTERNS = [
    r"\.env\b",
    r"credentials\.json",
    r"secrets\.yaml",
    r"secrets\.yml",
    r"\.pem\b",
    r"id_rsa",
]


class EnhancedRegexOutputClassifier:
    """Extended output classifier — baseline patterns plus PII, paths, env vars."""

    name = "enhanced-regex-ner-output"

    async def classify(self, text: str) -> ClassifyResult:
        _, timer = measure_latency()
        lower = text.lower()

        # Baseline: skill names (3+ threshold)
        skill_hits = sum(1 for s in _SKILL_NAMES if s in lower)
        if skill_hits >= 3:
            return ClassifyResult(
                label="unsafe", score=1.0,
                latency_ms=timer.stop(), classifier=self.name,
            )

        # Baseline: agent names (2+ threshold)
        agent_hits = sum(1 for a in _AGENT_NAMES if a in text)
        if agent_hits >= 2:
            return ClassifyResult(
                label="unsafe", score=1.0,
                latency_ms=timer.stop(), classifier=self.name,
            )

        # Baseline: leaked phrases
        for phrase in _LEAKED_PHRASES:
            if phrase.lower() in lower:
                return ClassifyResult(
                    label="unsafe", score=1.0,
                    latency_ms=timer.stop(), classifier=self.name,
                )

        # All regex pattern groups
        all_patterns = (
            _SANITIZE_PATTERNS + _PII_PATTERNS + _PATH_PATTERNS
            + _ENV_PATTERNS + _CONFIG_FILE_PATTERNS
        )
        for pattern in all_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return ClassifyResult(
                    label="unsafe", score=1.0,
                    latency_ms=timer.stop(), classifier=self.name,
                )

        return ClassifyResult(
            label="safe", score=1.0,
            latency_ms=timer.stop(), classifier=self.name,
        )
