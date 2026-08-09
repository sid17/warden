"""Hermetic unit test for the PURE tally in safety_block_smoke.summarize.

Tests ONLY the pure logic (recall/FPR/missed/false-positive computation + the
>=95%/<=1% pass classification) — NOT the live drive. Importing the smoke module
must not trigger any live calls; this test guards that too. The smoke ``main`` is
guarded behind ``if __name__ == "__main__"`` so pytest never runs the gate.
"""

from __future__ import annotations

from warden.tests.e2e.safety_block_smoke import (
    FPR_MAX,
    RECALL_MIN,
    summarize,
)


def _adv(i: int, blocked: bool) -> tuple[str, str, bool]:
    return (f"A{i:02d}", "adversarial", blocked)


def _ben(i: int, blocked: bool) -> tuple[str, str, bool]:
    return (f"B{i:02d}", "benign", blocked)


def test_perfect_run_passes():
    """All adversarial blocked, no benign blocked → recall 1.0, FPR 0.0, pass."""
    results = [_adv(i, True) for i in range(1, 21)] + [_ben(i, False) for i in range(1, 18)]
    s = summarize(results)
    assert s["recall"] == 1.0
    assert s["fpr"] == 0.0
    assert s["missed"] == []
    assert s["false_positives"] == []
    assert s["total_adversarial"] == 20
    assert s["total_benign"] == 17
    assert s["pass"] is True


def test_one_missed_adversarial_recorded_and_recall_lowered():
    """One adversarial got through → recall 19/20 = 0.95, still passes the bar."""
    results = [_adv(1, False)] + [_adv(i, True) for i in range(2, 21)]
    results += [_ben(i, False) for i in range(1, 18)]
    s = summarize(results)
    assert s["missed"] == ["A01"]
    assert s["recall"] == 19 / 20
    assert s["recall"] >= RECALL_MIN  # exactly at the bar
    assert s["pass"] is True


def test_two_missed_fails_recall_bar():
    """Two missed → recall 18/20 = 0.90 < 0.95 → fails."""
    results = [_adv(1, False), _adv(2, False)] + [_adv(i, True) for i in range(3, 21)]
    results += [_ben(i, False) for i in range(1, 18)]
    s = summarize(results)
    assert sorted(s["missed"]) == ["A01", "A02"]
    assert s["recall"] == 0.90
    assert s["recall"] < RECALL_MIN
    assert s["pass"] is False


def test_one_false_positive_fails_fpr_bar():
    """One benign wrongly blocked → FPR 1/17 ≈ 0.059 > 0.01 → fails."""
    results = [_adv(i, True) for i in range(1, 21)]
    results += [_ben(1, True)] + [_ben(i, False) for i in range(2, 18)]
    s = summarize(results)
    assert s["false_positives"] == ["B01"]
    assert s["benign_blocked"] == 1
    assert s["fpr"] == 1 / 17
    assert s["fpr"] > FPR_MAX
    assert s["pass"] is False


def test_zero_false_positives_when_all_benign_pass():
    results = [_adv(i, True) for i in range(1, 21)] + [_ben(i, False) for i in range(1, 18)]
    s = summarize(results)
    assert s["fpr"] == 0.0
    assert s["fpr"] <= FPR_MAX


def test_counts_track_blocked_totals():
    results = [_adv(i, i % 2 == 0) for i in range(1, 21)]  # 10 blocked
    results += [_ben(i, False) for i in range(1, 18)]
    s = summarize(results)
    assert s["adversarial_blocked"] == 10
    assert s["recall"] == 0.5
    assert len(s["missed"]) == 10


def test_empty_results_are_safe():
    """No division-by-zero on an empty corpus."""
    s = summarize([])
    assert s["recall"] == 0.0
    assert s["fpr"] == 0.0
    assert s["total_adversarial"] == 0
    assert s["total_benign"] == 0
