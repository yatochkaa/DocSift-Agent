"""Презентер сравнения двух прогонов `/evals/compare`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .. import filters
from ..tokens import HeatCell, heat_cell
from .common import Delta, make_delta


@dataclass(frozen=True)
class CompareMetricRow:
    field_name: str
    metric: str
    a: HeatCell
    b: HeatCell
    delta: Delta


@dataclass(frozen=True)
class CompareStatRow:
    label: str
    a_text: str
    b_text: str
    delta: Delta


@dataclass(frozen=True)
class ComparePage:
    title: str
    run_a_title: str
    run_b_title: str
    summary_improved: tuple[str, ...]
    summary_degraded: tuple[str, ...]
    summary_text: str
    metrics: tuple[CompareMetricRow, ...]
    performance: tuple[CompareStatRow, ...]


def build_compare(
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
    *,
    metrics_a: Sequence[Mapping[str, Any]] | None = None,
    metrics_b: Sequence[Mapping[str, Any]] | None = None,
    metric_key: str = "f1",
) -> ComparePage:
    if metrics_a is None:
        metrics_a = run_a.get("metrics") or ()
    if metrics_b is None:
        metrics_b = run_b.get("metrics") or ()
    index_a = {str(m.get("field") or m.get("name")): m for m in metrics_a}
    index_b = {str(m.get("field") or m.get("name")): m for m in metrics_b}
    field_names = list(index_a) + [name for name in index_b if name not in index_a]

    rows: list[CompareMetricRow] = []
    improved: list[str] = []
    degraded: list[str] = []
    for name in field_names:
        a_value = _num(index_a.get(name, {}).get(metric_key))
        b_value = _num(index_b.get(name, {}).get(metric_key))
        delta = make_delta(b_value, a_value, kind="percent")
        rows.append(CompareMetricRow(name, metric_key.upper(), heat_cell(a_value), heat_cell(b_value), delta))
        if delta.raw is not None and delta.raw >= 0.01:
            improved.append(f"{name} {delta.text}")
        elif delta.raw is not None and delta.raw <= -0.01:
            degraded.append(f"{name} {delta.text}")

    performance = (
        CompareStatRow(
            "Средняя точность",
            filters.percent(run_a.get("accuracy")),
            filters.percent(run_b.get("accuracy")),
            make_delta(_num(run_b.get("accuracy")), _num(run_a.get("accuracy")), kind="percent"),
        ),
        CompareStatRow(
            "Длительность",
            filters.duration(run_a.get("duration_seconds")),
            filters.duration(run_b.get("duration_seconds")),
            make_delta(
                _num(run_b.get("duration_seconds")),
                _num(run_a.get("duration_seconds")),
                kind="duration",
                higher_is_better=False,
            ),
        ),
        CompareStatRow(
            "Стоимость",
            filters.usd(run_a.get("cost")),
            filters.usd(run_b.get("cost")),
            make_delta(_num(run_b.get("cost")), _num(run_a.get("cost")), kind="money", higher_is_better=False),
        ),
    )

    if improved and degraded:
        summary = f"Улучшилось полей: {len(improved)}, деградировало: {len(degraded)}."
    elif improved:
        summary = f"Улучшилось полей: {len(improved)}, деградаций нет."
    elif degraded:
        summary = f"Деградировало полей: {len(degraded)}, улучшений нет."
    else:
        summary = "Значимых различий между прогонами нет."

    return ComparePage(
        title="Сравнение прогонов",
        run_a_title=_run_title(run_a),
        run_b_title=_run_title(run_b),
        summary_improved=tuple(improved),
        summary_degraded=tuple(degraded),
        summary_text=summary,
        metrics=tuple(rows),
        performance=performance,
    )


def _run_title(run: Mapping[str, Any]) -> str:
    parts = [str(run.get("run_id") or run.get("id") or "—")]
    if run.get("strategy"):
        parts.append(str(run["strategy"]))
    if run.get("model"):
        parts.append(str(run["model"]))
    return " · ".join(parts)


def _num(value: Any) -> float | None:
    return None if value is None else float(value)
