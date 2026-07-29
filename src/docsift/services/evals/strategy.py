from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel

from docsift.core.config import ExtractionStrategy
from docsift.schemas.evals import (
    EvalRunReport,
)

STRATEGY_ENVELOPE_VERSION = "1"


class StrategyEnvelope(BaseModel):
    """Обёртка над ``EvalRunReport`` с метаданными стратегии сравнения.

    Стратегия и bypass_cache не входят в сам отчёт (схему ``EvalRunReport`` менять
    нельзя), поэтому для сравнения стратегий оборачиваем отчёт в этот конверт.
    """

    envelope_version: str = STRATEGY_ENVELOPE_VERSION
    strategy: ExtractionStrategy
    report: EvalRunReport


def save_strategy_report(envelope: StrategyEnvelope, path: str | Path) -> Path:
    """Сохранить конверт стратегии как JSON (атомарная запись)."""
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
            temporary.write(envelope.model_dump_json(indent=2))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination


def load_strategy_report(path: str | Path) -> StrategyEnvelope:
    return StrategyEnvelope.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _accuracy_for(report: EvalRunReport) -> float | None:
    """Средняя accuracy по всем полям отчёта, или None если данных нет."""
    accuracies = [fm.accuracy for fm in report.metrics.fields.values() if fm.accuracy is not None]
    if not accuracies:
        return None
    return sum(accuracies) / len(accuracies)


def _average_cost_per_document(report: EvalRunReport) -> Decimal | None:
    if report.sample_count == 0:
        return None
    return report.cost_usd / Decimal(report.sample_count) if report.cost_usd is not None else None


def _average_duration_per_document(report: EvalRunReport) -> float:
    return report.average_duration_seconds


class StrategyComparisonRow(BaseModel):
    """Одна строка сравнительной таблицы стратегий."""

    strategy: ExtractionStrategy
    provider_backend: str
    model: str
    average_accuracy: float | None = None
    cheap_coverage: float | None = None
    average_cost_usd: Decimal | None = None
    average_duration_seconds: float = 0.0
    savings_vs_expensive_pct: Decimal | None = None


class StrategyComparison(BaseModel):
    """Результат сравнения нескольких стратегий."""

    dataset_name: str
    dataset_version: str
    schema_version: str
    rows: list[StrategyComparisonRow]


def _cheap_coverage(envelope: StrategyEnvelope) -> float:
    """Доля документов, закрытых только дешёвой моделью.

    Для не-cascade стратегий (cheap_only / expensive_only) охват = 1.0:
    cheap_only — все дешёвой, expensive_only — ни одной дешёвой (0.0).
    Для cascade — доля, где дорогая модель не вызывалась, неизвестна из отчёта
    (EvalSampleResult не хранит этот флаг), поэтому считаем по succeeded выборкам,
    у которых cost_usd равен нулю (дешёвая модель бесплатна). Это приближение,
    но для честного сравнения из JSON это единственный доступный сигнал.
    """
    if envelope.strategy == "cheap_only":
        return 1.0
    if envelope.strategy == "expensive_only":
        return 0.0
    succeeded = [s for s in envelope.report.samples if s.status == "succeeded"]
    if not succeeded:
        return 0.0
    cheap = sum(1 for s in succeeded if s.cost_usd is not None and s.cost_usd == Decimal(0))
    return cheap / len(succeeded)


def compare_strategies(envelopes: list[StrategyEnvelope]) -> StrategyComparison:
    """Сравнить несколько отчётов стратегий в одну таблицу.

    Все конверты должны быть с одного датасета (имя/версия/схема).
    """
    if not envelopes:
        raise ValueError("At least one strategy envelope is required")

    dataset_keys = {
        (e.report.dataset_name, e.report.dataset_version, e.report.schema_version)
        for e in envelopes
    }
    if len(dataset_keys) > 1:
        raise ValueError(
            "Cannot compare strategy envelopes from different datasets"
        )

    dataset_name = envelopes[0].report.dataset_name
    dataset_version = envelopes[0].report.dataset_version
    schema_version = envelopes[0].report.schema_version

    expensive_rows = [e for e in envelopes if e.strategy == "expensive_only"]
    baseline_cost = (
        _average_cost_per_document(expensive_rows[0].report)
        if expensive_rows
        else None
    )

    rows: list[StrategyComparisonRow] = []
    for envelope in envelopes:
        report = envelope.report
        avg_cost = _average_cost_per_document(report)
        savings = None
        if baseline_cost is not None and avg_cost is not None and baseline_cost > 0:
            savings = ((baseline_cost - avg_cost) / baseline_cost * Decimal(100)).quantize(
                Decimal("0.01")
            )
        rows.append(
            StrategyComparisonRow(
                strategy=envelope.strategy,
                provider_backend=report.provider_backend,
                model=report.model,
                average_accuracy=_accuracy_for(report),
                cheap_coverage=_cheap_coverage(envelope),
                average_cost_usd=avg_cost,
                average_duration_seconds=_average_duration_per_document(report),
                savings_vs_expensive_pct=savings,
            )
        )

    return StrategyComparison(
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        schema_version=schema_version,
        rows=rows,
    )


def render_strategy_comparison_table(comparison: StrategyComparison) -> str:
    """Текстовая таблица сравнения стратегий для терминала."""

    def fmt_money(value: Decimal | None) -> str:
        return "—" if value is None else f"${value:.6f}"

    def fmt_pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.1f}%"

    def fmt_savings(value: Decimal | None) -> str:
        return "—" if value is None else f"{value}%"

    headers = (
        "Стратегия",
        "Провайдер",
        "Модель",
        "Accuracy",
        "Охват дешёвой",
        "Ср. стоимость",
        "Ср. время (с)",
        "Экономия vs expensive",
    )
    rows: list[tuple[str, ...]] = []
    for row in comparison.rows:
        rows.append(
            (
                row.strategy,
                row.provider_backend,
                row.model,
                f"{row.average_accuracy:.3f}" if row.average_accuracy is not None else "—",
                fmt_pct(row.cheap_coverage * 100 if row.cheap_coverage is not None else None),
                fmt_money(row.average_cost_usd),
                f"{row.average_duration_seconds:.3f}",
                fmt_savings(row.savings_vs_expensive_pct),
            )
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    def render_row(r: tuple[str, ...]) -> str:
        return " | ".join(r[i].ljust(widths[i]) for i in range(len(r)))

    separator = "-+-".join("-" * w for w in widths)
    header_line = render_row(headers)
    body = "\n".join(render_row(r) for r in rows)
    summary = (
        f"Датасет: {comparison.dataset_name}/{comparison.dataset_version} "
        f"(schema {comparison.schema_version})"
    )
    return f"{summary}\n\n{header_line}\n{separator}\n{body}"