"""Презентеры списка прогонов `/evals` и отчёта `/evals/{run_id}`.

Поля берутся из EvalRunReport / EvalSampleResult: dataset, dataset_version, strategy,
provider, model, prompt_version, documents, metrics (precision/recall/f1 по полям),
step_duration_totals, errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .. import filters
from ..tokens import HeatCell, heat_cell
from .common import Chip, EmptyState, Pagination, WaterfallRow, build_pagination, build_waterfall, status_chip


@dataclass(frozen=True)
class RunRow:
    run_id: str
    href: str
    date_text: str
    dataset_text: str
    strategy_chip: Chip
    provider_chip: Chip
    model: str
    prompt_version: str
    documents_text: str
    accuracy_text: str
    accuracy_tone: str
    duration_text: str
    cost_text: str


@dataclass(frozen=True)
class EvalsPage:
    title: str
    rows: tuple[RunRow, ...]
    pagination: Pagination
    empty: EmptyState | None
    base_query: str = ""


def build_evals(
    *, runs: Sequence[Mapping[str, Any]], total: int, page: int = 1, per_page: int = 20
) -> EvalsPage:
    rows = tuple(_run_row(run) for run in runs)
    return EvalsPage(
        title="Прогоны evals",
        rows=rows,
        pagination=build_pagination(page, per_page, total),
        empty=None if rows else EmptyState("flask-conical", "Прогонов пока нет", "К документам", "/documents"),
    )


def _run_row(run: Mapping[str, Any]) -> RunRow:
    accuracy = run.get("accuracy")
    cell = heat_cell(float(accuracy) if accuracy is not None else None)
    dataset = str(run.get("dataset") or "—")
    version = run.get("dataset_version")
    return RunRow(
        run_id=str(run.get("run_id") or run.get("id")),
        href=f"/evals/{run.get('run_id') or run.get('id')}",
        date_text=filters.ru_datetime(run.get("started_at") or run.get("created_at")),
        dataset_text=f"{dataset} · v{version}" if version else dataset,
        strategy_chip=Chip(str(run.get("strategy") or "—"), "accent"),
        provider_chip=Chip(str(run.get("provider") or "—"), "muted"),
        model=str(run.get("model") or "—"),
        prompt_version=str(run.get("prompt_version") or "—"),
        documents_text=filters.number(run.get("documents") or run.get("document_count") or 0),
        accuracy_text=cell.text,
        accuracy_tone=cell.tone,
        duration_text=filters.duration(run.get("duration_seconds")),
        cost_text=filters.usd(run.get("cost")),
    )


@dataclass(frozen=True)
class MetricRow:
    field_name: str
    precision: HeatCell
    recall: HeatCell
    f1: HeatCell


@dataclass(frozen=True)
class SampleRow:
    document_id: str
    href: str
    file_name: str
    status_chip: Chip
    duration_text: str
    accuracy_text: str


@dataclass(frozen=True)
class EvalReportPage:
    title: str
    run_id: str
    header: tuple[tuple[str, str], ...]
    accuracy_text: str
    duration_text: str
    cost_text: str
    documents_text: str
    verdict: str
    attention_metrics: tuple[MetricRow, ...]
    metrics: tuple[MetricRow, ...]
    waterfall: tuple[WaterfallRow, ...]
    samples: tuple[SampleRow, ...]
    errors: tuple[str, ...]
    metrics_empty: EmptyState | None


def build_eval_report(
    *,
    run: Mapping[str, Any],
    metrics: Sequence[Mapping[str, Any]] = (),
    samples: Sequence[Mapping[str, Any]] = (),
    step_duration_totals: Mapping[str, float] | None = None,
    errors: Sequence[str] = (),
) -> EvalReportPage:
    header = (
        ("Дата", filters.ru_datetime(run.get("started_at") or run.get("created_at"))),
        ("Датасет", str(run.get("dataset") or "—")),
        ("Версия датасета", str(run.get("dataset_version") or "—")),
        ("Стратегия", str(run.get("strategy") or "—")),
        ("Провайдер", str(run.get("provider") or "—")),
        ("Модель", str(run.get("model") or "—")),
        ("Версия промта", str(run.get("prompt_version") or "—")),
        ("Документов", filters.number(run.get("documents") or run.get("document_count") or 0)),
        ("Средняя точность", filters.percent(run.get("accuracy"))),
        ("Длительность", filters.duration(run.get("duration_seconds"))),
        ("Стоимость", filters.usd(run.get("cost"))),
    )

    metric_rows = tuple(
        MetricRow(
            field_name=_metric_label(str(m.get("field") or m.get("name") or "—")),
            precision=heat_cell(_num(m.get("precision"))),
            recall=heat_cell(_num(m.get("recall"))),
            f1=heat_cell(_num(m.get("f1"))),
        )
        for m in metrics
    )

    sample_rows = tuple(
        SampleRow(
            document_id=str(s.get("document_id") or ""),
            href=f"/documents/{s.get('document_id')}",
            file_name=str(s.get("file_name") or s.get("document_id") or "—"),
            status_chip=status_chip(s.get("status")),
            duration_text=filters.duration(s.get("duration_seconds")),
            accuracy_text=filters.percent(s.get("accuracy")),
        )
        for s in samples
    )

    accuracy = _num(run.get("accuracy"))
    if accuracy is None:
        verdict = "Для этого прогона нет сводной оценки."
    elif accuracy >= 0.95:
        verdict = "Прогон выглядит стабильным: критичных отклонений по сводной точности нет."
    elif accuracy >= 0.85:
        verdict = "Результат рабочий, но поля с минимальным F1 стоит проверить перед сменой модели или промта."
    else:
        verdict = "Качество ниже рабочего порога: сначала разберите слабые поля и ошибки документов."
    attention = tuple(sorted(metric_rows, key=lambda row: row.f1.value if row.f1.value is not None else -1.0)[:5])

    return EvalReportPage(
        title=f"Прогон {run.get('run_id') or run.get('id')}",
        run_id=str(run.get("run_id") or run.get("id")),
        header=header,
        accuracy_text=filters.percent(accuracy),
        duration_text=filters.duration(run.get("duration_seconds")),
        cost_text=filters.usd(run.get("cost")),
        documents_text=filters.number(run.get("documents") or run.get("document_count") or 0),
        verdict=verdict,
        attention_metrics=attention,
        metrics=metric_rows,
        waterfall=tuple(build_waterfall((step_duration_totals or {}).items())),
        samples=sample_rows,
        errors=tuple(str(e) for e in errors),
        metrics_empty=None if metric_rows else EmptyState("bar-chart-3", "Метрики не посчитаны", None, None),
    )



_METRIC_LABELS = {
    "date": "Дата документа", "number": "Номер документа", "currency": "Валюта",
    "document_type": "Тип документа", "total_amount": "Итоговая сумма", "vat_amount": "Сумма НДС",
    "supplier/inn": "ИНН поставщика", "supplier/kpp": "КПП поставщика", "supplier/name": "Поставщик",
    "buyer/inn": "ИНН покупателя", "buyer/kpp": "КПП покупателя", "buyer/name": "Покупатель",
    "line_items/name": "Позиции: наименование", "line_items/unit": "Позиции: единица измерения",
    "line_items/amount": "Позиции: сумма", "line_items/quantity": "Позиции: количество",
    "line_items/vat_rate": "Позиции: ставка НДС", "line_items/unit_price": "Позиции: цена",
    "line_items/vat_amount": "Позиции: сумма НДС",
}

def _metric_label(raw: str) -> str:
    key = raw.replace(".", "/").replace("[]", "")
    return _METRIC_LABELS.get(key, raw.replace("_", " "))

def _num(value: Any) -> float | None:
    return None if value is None else float(value)
