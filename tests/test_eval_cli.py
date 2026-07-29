from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import eval.compare as compare_cli
import eval.run as run_cli
from docsift.schemas.evals import (
    EvalPricing,
    EvalRunReport,
    EvalTokenUsage,
    EvaluationMetrics,
)


def _report() -> EvalRunReport:
    return EvalRunReport(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        dataset_name="accounting",
        dataset_version="v1",
        schema_version="1",
        provider="cloud",
        provider_backend="openai_compatible",
        model="test-model",
        prompt_version="v2",
        started_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        completed_at=datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
        limit=3,
        sample_count=3,
        succeeded_count=3,
        failed_count=0,
        pricing=EvalPricing(
            input_price_per_million=Decimal(1),
            output_price_per_million=Decimal(2),
        ),
        token_usage=EvalTokenUsage(input_tokens=100, output_tokens=20),
        cost_usd=Decimal("0.00014"),
        total_duration_seconds=60,
        metrics=EvaluationMetrics(),
        samples=[],
    )


def test_run_parser_defaults_to_local_strategy_and_rejects_non_positive_limit() -> None:
    parser = run_cli.build_parser()

    args = parser.parse_args([])

    assert args.provider == "local"
    assert args.strategy == "cheap_only"
    assert args.use_cache is False
    assert args.dataset == Path("datasets/accounting/v1")
    assert args.limit is None

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--limit", "0"])

    assert exc_info.value.code == 2


def test_progress_output_is_explicit(capsys: pytest.CaptureFixture[str]) -> None:
    run_cli.print_progress(2, 5)

    assert capsys.readouterr().out == "Обработано 2 из 5\n"


def test_default_report_path_contains_dataset_strategy_and_run_id() -> None:
    report = _report()
    path = run_cli.default_report_path(
        "cheap_only", report.dataset_name, report.dataset_version, report.run_id
    )

    assert path.parent == Path("var/eval-reports")
    assert "cheap_only" in path.name
    assert str(report.run_id) in path.name


@pytest.mark.asyncio
async def test_execute_uses_selected_profile_limit_and_output_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    report = _report()
    output_path = tmp_path / "reports" / "run.json"
    dataset = object()
    provider = object()

    class FakeSettings:
        llm_prompt_version = "v1"
        cascade_confidence_threshold = 0.85
        eval_cloud_input_price_per_million = Decimal(1)
        eval_cloud_output_price_per_million = Decimal(2)

        def eval_profile(self, profile: str) -> SimpleNamespace:
            captured["profile_config"] = profile
            return SimpleNamespace(
                input_price_per_million=Decimal(1),
                output_price_per_million=Decimal(2),
            )

    class FakeEngine:
        async def dispose(self) -> None:
            captured["disposed"] = True

    class FakeSessionContext:
        async def __aenter__(self) -> object:
            return "session"

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            captured["runner_kwargs"] = kwargs

        async def run(self, selected_dataset: object, *, limit: int | None) -> EvalRunReport:
            captured["dataset"] = selected_dataset
            captured["limit"] = limit
            return report

    def fake_build_provider(settings: object, profile: str) -> object:
        captured["provider_profile"] = profile
        return provider

    def fake_save_strategy_report(envelope: object, path: Path) -> Path:
        captured["saved_envelope"] = envelope
        captured["saved_path"] = path
        return path

    monkeypatch.setattr(run_cli, "Settings", FakeSettings)
    monkeypatch.setattr(run_cli, "build_eval_llm_provider", fake_build_provider)
    monkeypatch.setattr(run_cli, "load_dataset", lambda path: dataset)
    monkeypatch.setattr(run_cli, "build_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(
        run_cli,
        "build_session_factory",
        lambda engine: lambda: FakeSessionContext(),
    )
    monkeypatch.setattr(run_cli, "EvalRunRepository", lambda session: "repository")
    monkeypatch.setattr(run_cli, "TextExtractionService", lambda: "extractor")
    monkeypatch.setattr(run_cli, "EvalRunner", FakeRunner)
    monkeypatch.setattr(run_cli, "save_strategy_report", fake_save_strategy_report)
    monkeypatch.setattr(run_cli, "render_metrics_table", lambda value: "metrics table")

    result = await run_cli.execute(
        argparse.Namespace(
            provider="cloud",
            dataset=tmp_path / "dataset",
            limit=3,
            output=output_path,
            prompt_version="v2",
            strategy="cheap_only",
            use_cache=False,
            dump_raw=False,
        )
    )

    assert result == output_path
    assert captured["profile_config"] == "cloud"
    assert captured["provider_profile"] == "cloud"
    assert captured["dataset"] is dataset
    assert captured["limit"] == 3
    assert captured["disposed"] is True
    runner_kwargs = captured["runner_kwargs"]
    assert runner_kwargs["provider"] is provider
    assert runner_kwargs["provider_profile"] == "cloud"
    assert runner_kwargs["prompt_version"] == "v2"
    assert runner_kwargs["strategy"] == "cheap_only"
    assert runner_kwargs["bypass_cache"] is True
    assert "metrics table" in capsys.readouterr().out


def test_compare_cli_prints_table_and_returns_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = {Path("a.json"): object(), Path("b.json"): object()}
    comparison = object()
    monkeypatch.setattr(compare_cli, "load_report", lambda path: reports[path])
    monkeypatch.setattr(
        compare_cli,
        "compare_reports",
        lambda run_a, run_b: comparison,
    )
    monkeypatch.setattr(
        compare_cli,
        "render_comparison_table",
        lambda value: "comparison table",
    )

    assert compare_cli.main(["a.json", "b.json"]) == 0
    assert capsys.readouterr().out == "comparison table\n"

    monkeypatch.setattr(
        compare_cli,
        "load_report",
        lambda path: (_ for _ in ()).throw(ValueError("broken report")),
    )

    assert compare_cli.main(["a.json", "b.json"]) == 1
    assert "broken report" in capsys.readouterr().err
