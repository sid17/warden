"""Report generator — produce markdown comparison report from classifier experiment results."""

from __future__ import annotations

from datetime import datetime

from warden.safety.experiments.classifier_runner import compute_metrics


def generate_report(
    input_results: dict[str, list[dict]],
    output_results: dict[str, list[dict]],
) -> str:
    """Generate full markdown benchmark report."""
    sections = [
        "# Classifier Benchmark Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    if input_results:
        sections.append("## Input Classifiers")
        sections.append("")
        sections.append(_comparison_table(input_results))
        sections.append("")
        for name, results in input_results.items():
            sections.append(_detail_section(name, results))
        sections.append("")

    if output_results:
        sections.append("## Output Classifiers")
        sections.append("")
        sections.append(_comparison_table(output_results))
        sections.append("")
        for name, results in output_results.items():
            sections.append(_detail_section(name, results))
        sections.append("")

    sections.append(_recommendation(input_results, output_results))

    return "\n".join(sections)


def _comparison_table(results_by_clf: dict[str, list[dict]]) -> str:
    """Generate a markdown comparison table."""
    header = (
        "| Classifier | Accuracy | Precision | Recall | F1 | FPR | "
        "Latency p50 | Latency p95 |"
    )
    sep = "|---|---|---|---|---|---|---|---|"
    rows = [header, sep]

    for name, results in results_by_clf.items():
        m = compute_metrics(results)
        rows.append(
            f"| {name} "
            f"| {m['accuracy']:.1%} "
            f"| {m['precision']:.1%} "
            f"| {m['recall']:.1%} "
            f"| {m['f1']:.3f} "
            f"| {m['fpr']:.1%} "
            f"| {m['latency_p50_ms']:.1f}ms "
            f"| {m['latency_p95_ms']:.1f}ms |"
        )

    return "\n".join(rows)


def _detail_section(name: str, results: list[dict]) -> str:
    """Generate per-classifier detail section."""
    m = compute_metrics(results)
    lines = [
        f"### {name}",
        "",
        f"**Confusion Matrix:** TP={m['tp']}, TN={m['tn']}, FP={m['fp']}, FN={m['fn']}",
        "",
    ]

    # Misclassified entries
    misclassified = [r for r in results if not r["correct"]]
    if misclassified:
        lines.append("**Misclassified entries:**")
        lines.append("")
        for r in misclassified:
            lines.append(
                f"- `{r['entry_id']}` ({r['category']}): "
                f"expected {r['expected_label']}, got {r['predicted_label']} "
                f"(score={r['score']:.2f})"
            )
        lines.append("")
    else:
        lines.append("**Misclassified entries:** None")
        lines.append("")

    return "\n".join(lines)


def _recommendation(
    input_results: dict[str, list[dict]],
    output_results: dict[str, list[dict]],
) -> str:
    """Generate recommendation section."""
    lines = ["## Recommendation", ""]

    if input_results:
        best = _pick_best(input_results)
        lines.append(f"**Best input classifier:** {best['name']}")
        lines.append(
            f"- Recall: {best['recall']:.1%}, FPR: {best['fpr']:.1%}, "
            f"F1: {best['f1']:.3f}, Latency p50: {best['latency_p50_ms']:.1f}ms"
        )
        lines.append("")

    if output_results:
        best = _pick_best(output_results)
        lines.append(f"**Best output classifier:** {best['name']}")
        lines.append(
            f"- Recall: {best['recall']:.1%}, FPR: {best['fpr']:.1%}, "
            f"F1: {best['f1']:.3f}, Latency p50: {best['latency_p50_ms']:.1f}ms"
        )
        lines.append("")

    lines.append("**Selection criteria:** >95% recall, <1% FPR, lowest latency.")
    return "\n".join(lines)


def _pick_best(results_by_clf: dict[str, list[dict]]) -> dict:
    """Pick the best classifier based on recall > 95%, FPR < 1%, then lowest latency."""
    candidates = []
    for name, results in results_by_clf.items():
        m = compute_metrics(results)
        m["name"] = name
        candidates.append(m)

    # Filter to those meeting thresholds
    meets = [c for c in candidates if c["recall"] >= 0.95 and c["fpr"] <= 0.01]

    if meets:
        # Among qualifying, pick lowest latency
        return min(meets, key=lambda c: c["latency_p50_ms"])

    # If none meet thresholds, pick highest F1
    return max(candidates, key=lambda c: c["f1"])
