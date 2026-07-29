from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from docsift.schemas.evals import (
    EvalPricing,
    EvalRunReport,
    EvalTokenUsage,
    EvaluationMetrics,
    FieldMetrics,
)
from docsift.services.evals.reports import (
    compare_reports,
    load_report,
    render_comparison_table,
    render_metrics_table,
    save_report,
)

RUN_A_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_B_ID = UUID("00000000-0000-0000-0000-000000000002")


def _report(
    fields: dict[str, FieldMetrics] | None = None,
    *,
    run_id: UUID = RUN_A_ID,
    dataset_name: str = "accounting",
    dataset_version: str = "v1",
    schema_version: str = "1",
) -> EvalRunReport:
    return EvalRunReport(
        run_id=run_id,
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        schema_version=schema_version,
        provider="cloud",
        provider_backend="openai_compatible",
        model="test-model",
        prompt_version="v1",
        started_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
        sample_count=0,
        succeeded_count=0,
        failed_count=0,
        pricing=EvalPricing(
            input_price_per_million=Decimal("1.25"),
            output_price_per_million=Decimal("2.50"),
        ),
        token_usage=EvalTokenUsage(input_tokens=100, output_tokens=20),
        cost_usd=Decimal("0.000175"),
        total_duration_seconds=60.0,
        metrics=EvaluationMetrics(fields=fields or {}),
        samples=[],
    )


def test_save_and_load_report_round_trip(tmp_path: Path) -> None:
    report = _report({"number": FieldMetrics(matches=2, misses=1)})
    destination = tmp_path / "nested" / "run.json"

    saved_path = save_report(report, destination)
    loaded = load_report(destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert saved_path == destination
    assert loaded == report
    assert payload["run_id"] == str(RUN_A_ID)
    assert payload["started_at"] == "2026-07-25T10:00:00Z"
    assert payload["cost_usd"] == "0.000175"
    assert list(destination.parent.glob("*.tmp")) == []


def test_load_report_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_report(path)


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("dataset_name", "other"),
        ("dataset_version", "v2"),
        ("schema_version", "2"),
    ],
)
def test_compare_rejects_incompatible_reports(
    changed_field: str,
    changed_value: str,
) -> None:
    run_b_kwargs = {changed_field: changed_value, "run_id": RUN_B_ID}

    with pytest.raises(ValueError, match="different dataset"):
        compare_reports(_report(), _report(**run_b_kwargs))  # type: ignore[arg-type]


def test_compare_reports_classifies_every_field_and_keeps_raw_deltas() -> None:
    run_a = _report(
        {
            "improved": FieldMetrics(matches=1, mismatches=1),
            "regressed": FieldMetrics(matches=2),
            "unchanged": FieldMetrics(matches=2, misses=1),
            "mixed": FieldMetrics(matches=1, misses=1),
            "only_a": FieldMetrics(matches=1),
        }
    )
    run_b = _report(
        {
            "improved": FieldMetrics(matches=2),
            "regressed": FieldMetrics(matches=1, mismatches=1),
            "unchanged": FieldMetrics(matches=2, misses=1),
            "mixed": FieldMetrics(matches=2, hallucinations=3),
            "only_b": FieldMetrics(matches=1),
        },
        run_id=RUN_B_ID,
    )

    comparison = compare_reports(run_a, run_b)
    fields = {field.field: field for field in comparison.fields}

    assert list(fields) == [
        "improved",
        "mixed",
        "only_a",
        "only_b",
        "regressed",
        "unchanged",
    ]
    assert fields["improved"].status == "improved"
    assert fields["regressed"].status == "regressed"
    assert fields["unchanged"].status == "unchanged"
    assert fields["mixed"].status == "mixed"
    assert fields["only_a"].status == "mixed"
    assert fields["only_b"].status == "mixed"
    assert fields["improved"].delta_matches == 1
    assert fields["improved"].delta_mismatches == -1
    assert fields["improved"].delta_accuracy == pytest.approx(0.5)
    assert fields["mixed"].delta_accuracy == pytest.approx(0.5)
    assert fields["mixed"].delta_precision == pytest.approx(-0.6)
    assert fields["only_b"].run_a == FieldMetrics()


def test_terminal_tables_include_fields_metrics_and_comparison_status() -> None:
    run_a = _report({"number": FieldMetrics(matches=1, misses=1)})
    run_b = _report(
        {"number": FieldMetrics(matches=2)},
        run_id=RUN_B_ID,
    )

    metrics_table = render_metrics_table(run_a)
    comparison_table = render_comparison_table(compare_reports(run_a, run_b))

    assert "number" in metrics_table
    assert "0.500" in metrics_table
    assert "number" in comparison_table
    assert "openai_compatible/test-model" in comparison_table
    assert "Стоимость:" in comparison_table
    assert "Время:" in comparison_table
    assert "+0.500" in comparison_table
    assert "1/1/0/0" in comparison_table
    assert "2/0/0/0" in comparison_table
