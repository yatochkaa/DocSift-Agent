from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

_DEFAULT_RUN_ID = UUID("00000000-0000-0000-0000-000000000001")

from docsift.schemas.evals import (
    EvalPricing,
    EvalRunReport,
    EvalSampleResult,
    EvalTokenUsage,
    EvaluationMetrics,
    FieldMetrics,
)
from docsift.services.evals.strategy import (
    StrategyEnvelope,
    compare_strategies,
    load_strategy_report,
    render_strategy_comparison_table,
    save_strategy_report,
)


def _sample(
    sample_id: str,
    cost: Decimal | None,
    status: str = "succeeded",
) -> EvalSampleResult:
    return EvalSampleResult(
        sample_id=sample_id,
        status=status,  # type: ignore[arg-type]
        duration_seconds=1.0,
        token_usage=EvalTokenUsage(input_tokens=10, output_tokens=5),
        cost_usd=cost,
    )


def _report(
    strategy: str,
    *,
    samples: list[EvalSampleResult],
    cost_usd: Decimal | None,
    total_duration: float,
    fields: dict[str, FieldMetrics] | None = None,
    model: str = "test-model",
    run_id: UUID = _DEFAULT_RUN_ID,
) -> StrategyEnvelope:
    report = EvalRunReport(
        run_id=run_id,
        dataset_name="accounting",
        dataset_version="v1",
        schema_version="1",
        provider="cloud",
        provider_backend="openai_compatible",
        model=model,
        prompt_version="v1",
        started_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
        sample_count=len(samples),
        succeeded_count=sum(1 for s in samples if s.status == "succeeded"),
        failed_count=sum(1 for s in samples if s.status == "failed"),
        pricing=EvalPricing(
            input_price_per_million=Decimal(1),
            output_price_per_million=Decimal(2),
        ),
        token_usage=EvalTokenUsage(input_tokens=100, output_tokens=20),
        cost_usd=cost_usd,
        total_duration_seconds=total_duration,
        average_duration_seconds=total_duration / len(samples) if samples else 0,
        metrics=EvaluationMetrics(fields=fields or {}),
        samples=samples,
    )
    return StrategyEnvelope(strategy=strategy, report=report)  # type: ignore[arg-type]


def test_compare_strategies_builds_table_from_real_reports() -> None:
    """Сравнительная таблица корректно строится из подготовленных отчётов."""
    expensive = _report(
        "expensive_only",
        samples=[_sample("s1", Decimal("0.10")), _sample("s2", Decimal("0.12"))],
        cost_usd=Decimal("0.22"),
        total_duration=10.0,
        fields={"number": FieldMetrics(matches=1)},
        model="gpt-4o",
    )
    cheap = _report(
        "cheap_only",
        samples=[_sample("s1", Decimal(0)), _sample("s2", Decimal(0))],
        cost_usd=Decimal(0),
        total_duration=4.0,
        fields={"number": FieldMetrics(matches=1)},
        model="qwen2.5",
    )
    cascade = _report(
        "cascade",
        samples=[_sample("s1", Decimal(0)), _sample("s2", Decimal("0.05"))],
        cost_usd=Decimal("0.05"),
        total_duration=6.0,
        fields={"number": FieldMetrics(matches=2)},
        model="qwen2.5",
    )

    comparison = compare_strategies([expensive, cheap, cascade])

    assert comparison.dataset_name == "accounting"
    assert len(comparison.rows) == 3

    by_strategy = {r.strategy: r for r in comparison.rows}

    # Охват дешёвой: cheap_only = 100%, expensive_only = 0%, cascade = 50%
    assert by_strategy["cheap_only"].cheap_coverage == 1.0
    assert by_strategy["expensive_only"].cheap_coverage == 0.0
    assert by_strategy["cascade"].cheap_coverage == 0.5

    # Средняя стоимость
    assert by_strategy["expensive_only"].average_cost_usd == Decimal("0.11")
    assert by_strategy["cheap_only"].average_cost_usd == Decimal(0)
    assert by_strategy["cascade"].average_cost_usd == Decimal("0.025")

    # Экономия относительно expensive_only
    assert by_strategy["expensive_only"].savings_vs_expensive_pct == Decimal(0)
    assert by_strategy["cheap_only"].savings_vs_expensive_pct == Decimal(100)
    # cascade: (0.11 - 0.025) / 0.11 * 100 ≈ 77.27%
    assert by_strategy["cascade"].savings_vs_expensive_pct == Decimal("77.27")

    # Среднее время
    assert by_strategy["cheap_only"].average_duration_seconds == 2.0
    assert by_strategy["expensive_only"].average_duration_seconds == 5.0


def test_compare_strategies_rejects_different_datasets() -> None:

    a = _report("cheap_only", samples=[_sample("s1", Decimal(0))], cost_usd=Decimal(0),
                total_duration=1.0)
    b = _report("expensive_only", samples=[_sample("s1", Decimal("0.10"))],
                cost_usd=Decimal("0.10"), total_duration=1.0)
    # Override dataset_version on b
    b.report.dataset_version = "v2"

    import pytest

    with pytest.raises(ValueError, match="different datasets"):
        compare_strategies([a, b])


def test_strategy_envelope_save_load_round_trip(tmp_path) -> None:
    envelope = _report(
        "cheap_only",
        samples=[_sample("s1", Decimal(0))],
        cost_usd=Decimal(0),
        total_duration=1.0,
    )
    path = tmp_path / "strategy.json"

    save_strategy_report(envelope, path)
    loaded = load_strategy_report(path)

    assert loaded.strategy == "cheap_only"
    assert loaded.report.dataset_name == "accounting"


def test_render_strategy_comparison_table_contains_all_columns() -> None:
    expensive = _report(
        "expensive_only",
        samples=[_sample("s1", Decimal("0.10"))],
        cost_usd=Decimal("0.10"),
        total_duration=5.0,
        model="gpt-4o",
    )
    cheap = _report(
        "cheap_only",
        samples=[_sample("s1", Decimal(0))],
        cost_usd=Decimal(0),
        total_duration=2.0,
        model="qwen2.5",
    )

    comparison = compare_strategies([expensive, cheap])
    table = render_strategy_comparison_table(comparison)

    assert "cheap_only" in table
    assert "expensive_only" in table
    assert "gpt-4o" in table
    assert "qwen2.5" in table
    assert "Экономия" in table
    assert "Охват" in table
    assert "100.0%" in table  # cheap coverage 100%
    assert "0.0%" in table  # expensive coverage 0%