from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from docsift.schemas.evals import (
    EvalFieldComparison,
    EvalRunComparison,
    EvalRunReport,
    FieldMetrics,
)

_EPSILON = 1e-12
_STATUS_LABELS = {
    "improved": "улучшилось",
    "regressed": "сломалось",
    "unchanged": "без изменений",
    "mixed": "смешанно",
}


def save_report(report: EvalRunReport, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(report.model_dump_json(indent=2))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def load_report(path: str | Path) -> EvalRunReport:
    return EvalRunReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _metric_delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return right - left


def _comparison_status(deltas: tuple[float | None, ...]) -> str:
    available = [delta for delta in deltas if delta is not None and abs(delta) > _EPSILON]
    if not available:
        return "unchanged"
    has_improvement = any(delta > 0 for delta in available)
    has_regression = any(delta < 0 for delta in available)
    if has_improvement and has_regression:
        return "mixed"
    return "improved" if has_improvement else "regressed"


def compare_reports(run_a: EvalRunReport, run_b: EvalRunReport) -> EvalRunComparison:
    compatibility_a = (run_a.dataset_name, run_a.dataset_version, run_a.schema_version)
    compatibility_b = (run_b.dataset_name, run_b.dataset_version, run_b.schema_version)
    if compatibility_a != compatibility_b:
        raise ValueError(
            "Cannot compare reports from different dataset names, versions, or schema versions"
        )

    comparisons: list[EvalFieldComparison] = []
    field_names = sorted(run_a.metrics.fields.keys() | run_b.metrics.fields.keys())
    for field_name in field_names:
        field_in_a = field_name in run_a.metrics.fields
        field_in_b = field_name in run_b.metrics.fields
        left = run_a.metrics.fields.get(field_name, FieldMetrics())
        right = run_b.metrics.fields.get(field_name, FieldMetrics())
        delta_accuracy = _metric_delta(left.accuracy, right.accuracy)
        delta_precision = _metric_delta(left.precision, right.precision)
        delta_recall = _metric_delta(left.recall, right.recall)
        status = (
            "mixed"
            if field_in_a != field_in_b
            else _comparison_status(
                (delta_accuracy, delta_precision, delta_recall)
            )
        )
        comparisons.append(
            EvalFieldComparison(
                field=field_name,
                status=status,
                run_a=left,
                run_b=right,
                delta_matches=right.matches - left.matches,
                delta_misses=right.misses - left.misses,
                delta_hallucinations=right.hallucinations - left.hallucinations,
                delta_mismatches=right.mismatches - left.mismatches,
                accuracy_a=left.accuracy,
                accuracy_b=right.accuracy,
                delta_accuracy=delta_accuracy,
                precision_a=left.precision,
                precision_b=right.precision,
                delta_precision=delta_precision,
                recall_a=left.recall,
                recall_b=right.recall,
                delta_recall=delta_recall,
            )
        )
    return EvalRunComparison(
        dataset_name=run_a.dataset_name,
        dataset_version=run_a.dataset_version,
        schema_version=run_a.schema_version,
        run_a_id=run_a.run_id,
        run_b_id=run_b.run_id,
        provider_a=run_a.provider_backend,
        provider_b=run_b.provider_backend,
        model_a=run_a.model,
        model_b=run_b.model,
        prompt_version_a=run_a.prompt_version,
        prompt_version_b=run_b.prompt_version,
        cost_usd_a=run_a.cost_usd,
        cost_usd_b=run_b.cost_usd,
        total_duration_seconds_a=run_a.total_duration_seconds,
        total_duration_seconds_b=run_b.total_duration_seconds,
        fields=comparisons,
    )


def _format_metric(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def _render_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render_row(row: tuple[str, ...]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join((render_row(headers), separator, *(render_row(row) for row in rows)))


def render_metrics_table(report: EvalRunReport) -> str:
    rows = []
    for field_name, metrics in sorted(report.metrics.fields.items()):
        rows.append(
            (
                field_name,
                str(metrics.matches),
                str(metrics.misses),
                str(metrics.hallucinations),
                str(metrics.mismatches),
                _format_metric(metrics.accuracy),
                _format_metric(metrics.precision),
                _format_metric(metrics.recall),
            )
        )
    return _render_table(
        ("Поле", "Совп.", "Проп.", "Галлюц.", "Ошиб.", "Accuracy", "Precision", "Recall"),
        rows,
    )


def render_comparison_table(comparison: EvalRunComparison) -> str:
    rows = []
    for field in comparison.fields:
        counts_a = (
            f"{field.run_a.matches}/{field.run_a.misses}/"
            f"{field.run_a.hallucinations}/{field.run_a.mismatches}"
        )
        counts_b = (
            f"{field.run_b.matches}/{field.run_b.misses}/"
            f"{field.run_b.hallucinations}/{field.run_b.mismatches}"
        )
        rows.append(
            (
                field.field,
                _STATUS_LABELS[field.status],
                counts_a,
                counts_b,
                _format_metric(field.delta_accuracy, signed=True),
                _format_metric(field.delta_precision, signed=True),
                _format_metric(field.delta_recall, signed=True),
            )
        )
    table = _render_table(
        (
            "Поле",
            "Результат",
            "A: M/Пр/Г/О",
            "B: M/Пр/Г/О",
            "Δ accuracy",
            "Δ precision",
            "Δ recall",
        ),
        rows,
    )
    cost_a = "—" if comparison.cost_usd_a is None else f"${comparison.cost_usd_a:.6f}"
    cost_b = "—" if comparison.cost_usd_b is None else f"${comparison.cost_usd_b:.6f}"
    summary = (
        f"A: {comparison.provider_a}/{comparison.model_a}, "
        f"prompt={comparison.prompt_version_a}\n"
        f"B: {comparison.provider_b}/{comparison.model_b}, "
        f"prompt={comparison.prompt_version_b}\n"
        f"Стоимость: {cost_a} -> {cost_b}\n"
        f"Время: {comparison.total_duration_seconds_a:.3f}s -> "
        f"{comparison.total_duration_seconds_b:.3f}s"
    )
    return f"{summary}\n\n{table}"
