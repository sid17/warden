"""Classifier protocol and result types for safety middleware."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ClassifyResult:
    """Result from a classifier."""

    label: str  # "safe" | "unsafe" | "uncertain"
    score: float  # 0.0-1.0 confidence
    latency_ms: float
    classifier: str  # name for reporting


@runtime_checkable
class Classifier(Protocol):
    """Protocol all classifiers implement — the unit of swapping."""

    name: str

    async def classify(self, text: str) -> ClassifyResult: ...


def measure_latency() -> tuple[float, "LatencyTimer"]:
    """Helper to measure classify() wall-clock time.

    Usage:
        start, timer = measure_latency()
        # ... do work ...
        ms = timer.stop()
    """
    start = time.perf_counter()
    return start, LatencyTimer(start)


class LatencyTimer:
    """Simple timer that returns elapsed ms on stop()."""

    def __init__(self, start: float) -> None:
        self._start = start

    def stop(self) -> float:
        return (time.perf_counter() - self._start) * 1000
