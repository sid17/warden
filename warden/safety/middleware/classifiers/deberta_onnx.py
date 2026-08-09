"""DeBERTa-v3 classifier via HuggingFace transformers.

Uses the ProtectAI prompt injection v2 model. Originally planned for ONNX
Runtime, but the repo only has safetensors — so we use the transformers
pipeline directly. ONNX export can be added later if latency optimization
is needed.
"""

from __future__ import annotations

from warden.safety.middleware.classifiers import ClassifyResult, measure_latency

_MODEL_ID = "protectai/deberta-v3-base-prompt-injection-v2"


class DeBertaONNXClassifier:
    """DeBERTa-v3 — ProtectAI prompt injection detector."""

    name = "deberta-v3-onnx"

    def __init__(self) -> None:
        from transformers import pipeline  # type: ignore[import-untyped]

        self._pipe = pipeline(
            "text-classification",
            model=_MODEL_ID,
            top_k=None,
        )

    async def classify(self, text: str) -> ClassifyResult:
        _, timer = measure_latency()
        results = self._pipe(text, truncation=True, max_length=512)
        ms = timer.stop()

        scores_by_label = {r["label"]: r["score"] for r in results[0]}

        # ProtectAI labels: SAFE, INJECTION
        safe_score = scores_by_label.get("SAFE", 0.0)
        injection_score = scores_by_label.get("INJECTION", 0.0)

        if injection_score > safe_score:
            return ClassifyResult(
                label="unsafe",
                score=injection_score,
                latency_ms=ms,
                classifier=self.name,
            )
        return ClassifyResult(
            label="safe",
            score=safe_score,
            latency_ms=ms,
            classifier=self.name,
        )
