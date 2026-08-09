"""PromptGuard 2 22M classifier via HuggingFace transformers."""

from __future__ import annotations

from warden.safety.middleware.classifiers import ClassifyResult, measure_latency

_MODEL_ID = "meta-llama/Llama-Prompt-Guard-2-22M"


class PromptGuard22MClassifier:
    """PromptGuard 2 22M — smaller, faster prompt injection detector."""

    name = "promptguard-22m"

    def __init__(self) -> None:
        from transformers import pipeline  # type: ignore[import-untyped]

        try:
            self._pipe = pipeline(
                "text-classification",
                model=_MODEL_ID,
                top_k=None,
            )
        except OSError as e:
            raise RuntimeError(
                f"Cannot load {_MODEL_ID}. This is a gated model — "
                "run `huggingface-cli login` with a token that has access. "
                f"Original error: {e}"
            ) from e

    async def classify(self, text: str) -> ClassifyResult:
        _, timer = measure_latency()
        results = self._pipe(text, truncation=True, max_length=512)
        ms = timer.stop()

        scores_by_label = {r["label"]: r["score"] for r in results[0]}

        # Labels vary by model source:
        # Official meta-llama: LABEL_0 (benign), LABEL_1 (malicious)
        # Gravitee ONNX export: BENIGN, MALICIOUS
        # Some versions: BENIGN, INJECTION, JAILBREAK
        benign_score = (
            scores_by_label.get("LABEL_0", 0.0)
            + scores_by_label.get("BENIGN", 0.0)
        )
        unsafe_score = (
            scores_by_label.get("LABEL_1", 0.0)
            + scores_by_label.get("MALICIOUS", 0.0)
            + scores_by_label.get("INJECTION", 0.0)
            + scores_by_label.get("JAILBREAK", 0.0)
        )

        if unsafe_score > benign_score:
            return ClassifyResult(
                label="unsafe",
                score=unsafe_score,
                latency_ms=ms,
                classifier=self.name,
            )
        return ClassifyResult(
            label="safe",
            score=benign_score,
            latency_ms=ms,
            classifier=self.name,
        )
