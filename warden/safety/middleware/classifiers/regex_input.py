"""Regex input classifier — wraps E3/E9 baseline patterns as Classifier."""

from __future__ import annotations

import base64
import re

from warden.safety.middleware.classifiers import ClassifyResult, measure_latency

# Base64-looking runs (>=16 chars of the b64 alphabet, optional padding). A cheap
# de-obfuscation step: base64-encoded injections (e.g. "SWdub3Jl…" ->
# "Ignore all previous instructions") evade raw substring matching, so we decode
# any such run and scan the DECODED text against the same patterns.
_B64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")

# Combined patterns from orchestrator/experiments.py E3 + E9
_INJECTION_PATTERNS = [
    # Original SanitizeMiddleware
    "ignore instructions",
    "ignore all previous",
    "disregard",
    "forget your instructions",
    # E3 expanded
    "override your",
    "prior directives",
    "new instructions",
    "you are now",
    "act as",
    "pretend you",
    "jailbreak",
    "unrestricted mode",
    "developer mode",
    "ignore previous",
    "bypass",
    # E9 injection phrases
    "override",
    "ignore",
    "forget",
]

_EXTRACTION_PATTERNS = [
    "what skills",
    "what agents",
    "what tools",
    "list your",
    "list all",
    "what can you do",
    "your capabilities",
    "your instructions",
    "show me your",
    "tell me about your",
    "what access",
    "your system prompt",
    "your configuration",
    "print them verbatim",
]

_SECRET_SEEKING_PATTERNS = [
    "password",
    "secret",
    "api_key",
    "api key",
    "private key",
    "credential",
    "access token",
]


_ALL_PATTERNS = _INJECTION_PATTERNS + _EXTRACTION_PATTERNS + _SECRET_SEEKING_PATTERNS


def _b64_decoded_texts(text: str) -> list[str]:
    """Return lowercased UTF-8 decodings of base64-looking runs in ``text``.

    Only runs that base64-decode to valid UTF-8 are yielded (garbage binary is
    skipped). This de-obfuscates base64-encoded injections so the same patterns
    catch them — a benign string virtually never base64-decodes to an injection
    phrase, so this adds recall without adding false positives.
    """
    out: list[str] = []
    for m in _B64_RUN.finditer(text):
        tok = m.group(0)
        try:
            decoded = base64.b64decode(tok + "=" * (-len(tok) % 4), validate=False)
            out.append(decoded.decode("utf-8").lower())
        except (ValueError, UnicodeDecodeError):
            continue
    return out


class RegexInputClassifier:
    """Baseline input classifier using E3/E9 substring matching.

    Scans the raw input AND any base64-decoded runs within it, so an injection
    hidden behind base64 encoding is caught by the same pattern set.
    """

    name = "regex-input-e3e9"

    async def classify(self, text: str) -> ClassifyResult:
        _, timer = measure_latency()
        candidates = [text.lower(), *_b64_decoded_texts(text)]

        for cand in candidates:
            for pattern in _ALL_PATTERNS:
                if pattern in cand:
                    return ClassifyResult(
                        label="unsafe",
                        score=1.0,
                        latency_ms=timer.stop(),
                        classifier=self.name,
                    )

        return ClassifyResult(
            label="safe",
            score=1.0,
            latency_ms=timer.stop(),
            classifier=self.name,
        )
