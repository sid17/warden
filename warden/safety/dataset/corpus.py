"""Corpus loader — load JSON test corpora for safety evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DATASET_DIR = Path(__file__).parent


@dataclass
class CorpusEntry:
    """Single entry from a test corpus."""

    id: str
    text: str
    label: str  # "benign"|"adversarial" for input, "leaked"|"clean" for output
    category: str


def _load_corpus(filename: str) -> list[CorpusEntry]:
    path = _DATASET_DIR / filename
    with open(path) as f:
        data = json.load(f)
    return [CorpusEntry(**entry) for entry in data]


def load_input_corpus() -> list[CorpusEntry]:
    """Load input test corpus (benign/adversarial queries)."""
    return _load_corpus("corpus_input.json")


def load_output_corpus() -> list[CorpusEntry]:
    """Load output test corpus (leaked/clean responses)."""
    return _load_corpus("corpus_output.json")
