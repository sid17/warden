"""Ollama output classifier — detects information leakage via local LLM."""

from __future__ import annotations

import json

import httpx

from warden.safety.middleware.classifiers import ClassifyResult, measure_latency

_DEFAULT_MODEL = "gemma3:4b"
_DEFAULT_BASE_URL = "http://localhost:11434"

_SYSTEM_PROMPT = (
    "You are an information leakage classifier. Analyze the given text and determine "
    "if it leaks internal system information.\n\n"
    "Respond with ONLY a JSON object (no other text):\n"
    '{"label": "safe" or "unsafe", "confidence": 0.0-1.0}\n\n'
    "Examples of leaked information (unsafe):\n"
    "- Listing internal skill names (kickoff, build-phase, plan-phase, etc.)\n"
    "- Revealing agent types (Explore, general-purpose, Plan)\n"
    "- Exposing .claude/ file paths or SKILL.md contents\n"
    "- Showing YAML frontmatter from internal config files\n"
    "- Revealing absolute user paths (/Users/...)\n"
    "- Describing internal system prompts or middleware\n\n"
    "Safe responses just answer user questions without revealing internals.\n"
)


class OllamaGuardOutputClassifier:
    """Output classifier using Ollama-hosted LLM for leakage detection."""

    name = "ollama-gemma3-output"

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._model = model
        self._base_url = base_url

    async def classify(self, text: str) -> ClassifyResult:
        _, timer = measure_latency()

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate",
                    json={
                        "model": self._model,
                        "prompt": text,
                        "system": _SYSTEM_PROMPT,
                        "stream": False,
                        "options": {"temperature": 0.0},
                    },
                )
                resp.raise_for_status()
        except httpx.ConnectError as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self._base_url}. "
                f"Is Ollama running? Error: {e}"
            ) from e

        ms = timer.stop()
        body = resp.json()
        raw = body.get("response", "")

        label, score = _parse_response(raw)
        return ClassifyResult(
            label=label,
            score=score,
            latency_ms=ms,
            classifier=self.name,
        )


def _parse_response(raw: str) -> tuple[str, float]:
    """Parse LLM JSON response to (label, score)."""
    text = raw
    if "</think>" in text:
        text = text.split("</think>")[-1]
    text = text.strip()

    try:
        data = json.loads(text)
        label = data.get("label", "uncertain")
        score = float(data.get("confidence", 0.5))
        if label not in ("safe", "unsafe"):
            label = "uncertain"
        return label, score
    except (json.JSONDecodeError, ValueError, TypeError):
        lower = text.lower()
        if "unsafe" in lower:
            return "unsafe", 0.5
        if "safe" in lower:
            return "safe", 0.5
        return "uncertain", 0.0
