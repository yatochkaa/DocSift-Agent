r"""Тесты пошаговых таймингов конвейера.

Куда класть: tests/test_step_timings.py

Запуск::

    .\.venv\Scripts\python.exe -m pytest tests/test_step_timings.py -v --basetemp=.pytest-tmp

Сеть и модель не нужны: провайдер и извлекатель текста подменены фейками,
как в tests/test_eval_runner.py.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from docsift.core.timing import (
    STEP_LLM_EXTRACTION,
    STEP_METRICS,
    STEP_TEXT_EXTRACTION,
    StepTimings,
    merge_step_durations,
)
from docsift.db.models import EvalRun
from docsift.schemas.evals import (
    DatasetManifest,
    EvalPricing,
    EvalRunReport,
    ExpectedDocument,
)
from docsift.schemas.llm import LLMRequest, LLMResponse
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.evals.dataset import Dataset, DatasetSample
from docsift.services.evals.runner import EvalRunner
from eval.run import render_step_durations

# ---------------------------------------------------------------------------
# StepTimings
# ---------------------------------------------------------------------------


def test_measure_records_a_positive_duration() -> None:
    timings = StepTimings()

    with timings.measure(STEP_TEXT_EXTRACTION):
        time.sleep(0.01)

    recorded = timings.as_dict()
    assert list(recorded) == [STEP_TEXT_EXTRACTION]
    assert recorded[STEP_TEXT_EXTRACTION] >= 0.005


def test_repeated_steps_are_summed() -> None:
    """Каскад вызывает модель дважды -- шаг должен остаться одним ключом."""
    timings = StepTimings()

    timings.add(STEP_LLM_EXTRACTION, 1.5)
    timings.add(STEP_LLM_EXTRACTION, 2.25)

    assert timings.as_dict() == {STEP_LLM_EXTRACTION: 3.75}
    assert len(timings) == 1


def test_step_order_follows_first_launch() -> None:
    timings = StepTimings()

    timings.add(STEP_TEXT_EXTRACTION, 1)
    timings.add(STEP_LLM_EXTRACTION, 2)
    timings.add(STEP_TEXT_EXTRACTION, 3)

    assert list(timings.as_dict()) == [STEP_TEXT_EXTRACTION, STEP_LLM_EXTRACTION]


def test_duration_is_recorded_even_when_the_step_raises() -> None:
    """Самое важное свойство: аварийный шаг всё равно попадает в замеры."""
    timings = StepTimings()

    with pytest.raises(RuntimeError, match="boom"), timings.measure(STEP_LLM_EXTRACTION):
        raise RuntimeError("boom")

    assert STEP_LLM_EXTRACTION in timings
    assert timings.as_dict()[STEP_LLM_EXTRACTION] >= 0


def test_as_dict_returns_a_copy() -> None:
    timings = StepTimings()
    timings.add(STEP_METRICS, 1)

    snapshot = timings.as_dict()
    snapshot[STEP_METRICS] = 999
    snapshot["чужой шаг"] = 1

    assert timings.as_dict() == {STEP_METRICS: 1.0}


def test_negative_duration_is_clamped_to_zero() -> None:
    """Отрицательное значение сломало бы валидацию отчёта."""
    timings = StepTimings()

    timings.add(STEP_METRICS, -5)

    assert timings.as_dict() == {STEP_METRICS: 0.0}


def test_empty_step_name_is_rejected() -> None:
    timings = StepTimings()

    with pytest.raises(ValueError, match="must not be empty"):
        timings.add("   ", 1)


def test_merge_step_durations_accumulates_across_documents() -> None:
    total: dict[str, float] = {}

    merge_step_durations(total, {STEP_TEXT_EXTRACTION: 1.5, STEP_LLM_EXTRACTION: 10.0})
    merge_step_durations(total, {STEP_LLM_EXTRACTION: 5.5, STEP_METRICS: 0.25})

    assert total == {
        STEP_TEXT_EXTRACTION: 1.5,
        STEP_LLM_EXTRACTION: 15.5,
        STEP_METRICS: 0.25,
    }


# ---------------------------------------------------------------------------
# Вывод CLI
# ---------------------------------------------------------------------------


def test_step_line_is_sorted_by_duration_and_shows_shares() -> None:
    line = render_step_durations(
        {STEP_TEXT_EXTRACTION: 3.0, STEP_LLM_EXTRACTION: 97.0},
        100.0,
    )

    assert line.startswith("Шаги: " + STEP_LLM_EXTRACTION)
    assert "llm_extraction=97.000s (97%)" in line
    assert "text_extraction=3.000s (3%)" in line


def test_step_line_shows_unmeasured_remainder() -> None:
    """Разница между общим временем и суммой шагов должна быть видна."""
    line = render_step_durations({STEP_LLM_EXTRACTION: 60.0}, 100.0)

    assert "прочее=40.000s (40%)" in line


def test_step_line_survives_an_empty_report() -> None:
    assert render_step_durations({}, 0.0) == "Шаги: замеров нет"
    assert "прочее" not in render_step_durations({STEP_METRICS: 1.0}, 0.0)


# ---------------------------------------------------------------------------
# Схема отчёта
# ---------------------------------------------------------------------------


def test_old_reports_without_timings_still_load() -> None:
    """Отчёты версии 2 лежат в var/eval-reports и должны читаться по-прежнему."""
    payload = {
        "report_version": "2",
        "run_id": "9bc97aed-5b38-4408-a441-acf65e0cabb0",
        "dataset_name": "accounting",
        "dataset_version": "v1",
        "schema_version": "1",
        "provider": "local",
        "provider_backend": "ollama",
        "model": "qwen2.5-coder:7b",
        "prompt_version": "v3",
        "temperature": 0,
        "started_at": "2026-07-25T09:22:31.601943Z",
        "completed_at": "2026-07-25T09:28:39.557223Z",
        "limit": 1,
        "sample_count": 1,
        "succeeded_count": 1,
        "failed_count": 0,
        "pricing": {
            "currency": "USD",
            "input_price_per_million": "0",
            "output_price_per_million": "0",
        },
        "token_usage": {"input_tokens": 6020, "output_tokens": 2034},
        "cost_usd": "0",
        "total_duration_seconds": 367.95578060000844,
        "average_duration_seconds": 367.8827017000003,
        "metrics": {"fields": {}},
        "samples": [
            {
                "sample_id": "doc_01",
                "status": "succeeded",
                "duration_seconds": 367.8827017000003,
                "token_usage": {"input_tokens": 6020, "output_tokens": 2034},
                "cost_usd": "0",
                "metrics": {"fields": {}},
            }
        ],
    }

    report = EvalRunReport.model_validate(payload)

    assert report.report_version == "2"
    assert report.step_duration_totals == {}
    assert report.samples[0].step_durations == {}


def test_timings_survive_a_json_round_trip() -> None:
    payload = {
        "report_version": "3",
        "run_id": "9bc97aed-5b38-4408-a441-acf65e0cabb0",
        "dataset_name": "accounting",
        "dataset_version": "v1",
        "schema_version": "1",
        "provider": "local",
        "provider_backend": "ollama",
        "model": "qwen2.5-coder:7b",
        "prompt_version": "v3",
        "temperature": 0,
        "started_at": "2026-07-25T09:22:31.601943Z",
        "completed_at": "2026-07-25T09:28:39.557223Z",
        "sample_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "pricing": {
            "currency": "USD",
            "input_price_per_million": "0",
            "output_price_per_million": "0",
        },
        "token_usage": {"input_tokens": None, "output_tokens": None},
        "total_duration_seconds": 10.0,
        "step_duration_totals": {STEP_TEXT_EXTRACTION: 1.25, STEP_LLM_EXTRACTION: 8.5},
        "metrics": {"fields": {}},
        "samples": [],
    }

    restored = EvalRunReport.model_validate_json(
        EvalRunReport.model_validate(payload).model_dump_json()
    )

    assert restored.step_duration_totals == {
        STEP_TEXT_EXTRACTION: 1.25,
        STEP_LLM_EXTRACTION: 8.5,
    }


# ---------------------------------------------------------------------------
# Интеграция с прогоном
# ---------------------------------------------------------------------------


def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "source",
    }


def _field(value: Any) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": 0.99, "sources": [_source()]}


def _valid_payload() -> dict[str, Any]:
    return {
        "document_type": _field("payment_invoice"),
        "number": _field("42"),
        "date": _field("2026-01-10"),
        "supplier": {
            "name": _field("ООО Поставщик"),
            "inn": _field("7707083893"),
            "kpp": _field("773601001"),
        },
        "buyer": {
            "name": _field("ИП Покупатель"),
            "inn": _field("500100732259"),
            "kpp": _field(None),
        },
        "total_amount": _field("1200.00"),
        "vat_amount": _field("200.00"),
        "currency": _field("RUB"),
        "line_items": [],
    }


def _dataset(tmp_path: Path, count: int) -> Dataset:
    expected = ExpectedDocument(number="42")
    samples = tuple(
        DatasetSample(
            sample_id=f"sample-{index}",
            document_path=tmp_path / f"sample-{index}.pdf",
            expected_path=tmp_path / f"sample-{index}.expected.json",
            expected=expected,
        )
        for index in range(1, count + 1)
    )
    return Dataset(
        root=tmp_path,
        manifest=DatasetManifest(name="accounting", version="v1"),
        samples=samples,
    )


class FakeProvider:
    provider_name = "fake"
    model_name = "fake-model"
    supports_json_schema = True

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._responses[len(self.requests) - 1]


class FakeTextExtractor:
    def __init__(self, failing_names: set[str] | None = None) -> None:
        self.failing_names = failing_names or set()

    def extract(self, source_path: str | Path) -> TextExtractionResult:
        path = Path(source_path)
        if path.name in self.failing_names:
            raise RuntimeError("text extraction failed")
        return TextExtractionResult(
            source_path=str(path),
            media_type="application/pdf",
            pages=[
                ExtractedPage(
                    number=1,
                    width=100,
                    height=100,
                    blocks=[
                        TextBlock(
                            text="Счёт №42",
                            bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                            confidence=1,
                            source="pdf:text_layer",
                        )
                    ],
                )
            ],
            used_ocr=False,
        )


class FakeEvalRunRepository:
    def __init__(self) -> None:
        self.updated: list[EvalRun] = []

    async def create(self, eval_run: EvalRun) -> EvalRun:
        return eval_run

    async def update(self, eval_run: EvalRun) -> EvalRun:
        self.updated.append(eval_run)
        return eval_run


def _runner(provider: FakeProvider, extractor: FakeTextExtractor) -> EvalRunner:
    return EvalRunner(
        provider=provider,
        provider_profile="local",
        pricing=EvalPricing(
            input_price_per_million=Decimal(0),
            output_price_per_million=Decimal(0),
        ),
        prompt_version="v1",
        text_extractor=extractor,
        run_repository=FakeEvalRunRepository(),
    )


@pytest.mark.asyncio
async def test_successful_document_reports_every_step(tmp_path: Path) -> None:
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), None, None)]
    )

    report = await _runner(provider, FakeTextExtractor()).run(_dataset(tmp_path, 1))

    steps = report.samples[0].step_durations
    assert set(steps) == {STEP_TEXT_EXTRACTION, STEP_LLM_EXTRACTION, STEP_METRICS}
    assert all(value >= 0 for value in steps.values())
    assert sum(steps.values()) <= report.samples[0].duration_seconds + 0.05


@pytest.mark.asyncio
async def test_failed_document_reports_the_step_it_died_on(tmp_path: Path) -> None:
    """Ради этого всё и затевалось: видно, что документ встал на извлечении текста."""
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), None, None)]
    )
    extractor = FakeTextExtractor({"sample-1.pdf"})

    report = await _runner(provider, extractor).run(_dataset(tmp_path, 2))

    failed, succeeded = report.samples
    assert failed.status == "failed"
    assert set(failed.step_durations) == {STEP_TEXT_EXTRACTION}
    assert set(succeeded.step_durations) == {
        STEP_TEXT_EXTRACTION,
        STEP_LLM_EXTRACTION,
        STEP_METRICS,
    }


@pytest.mark.asyncio
async def test_run_totals_are_the_sum_over_documents(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), None, None)
            for _ in range(2)
        ]
    )

    report = await _runner(provider, FakeTextExtractor()).run(_dataset(tmp_path, 2))

    for step in (STEP_TEXT_EXTRACTION, STEP_LLM_EXTRACTION, STEP_METRICS):
        expected = sum(sample.step_durations[step] for sample in report.samples)
        assert report.step_duration_totals[step] == pytest.approx(expected, abs=1e-6)
    assert sum(report.step_duration_totals.values()) <= report.total_duration_seconds + 0.05


@pytest.mark.asyncio
async def test_timings_reach_the_saved_report(tmp_path: Path) -> None:
    """Замеры должны пережить сериализацию в JSON, иначе в отчёте их не будет."""
    provider = FakeProvider(
        [LLMResponse(json.dumps(_valid_payload(), ensure_ascii=False), None, None)]
    )

    report = await _runner(provider, FakeTextExtractor()).run(_dataset(tmp_path, 1))
    restored = EvalRunReport.model_validate_json(report.model_dump_json())

    assert restored.report_version == "3"
    assert restored.step_duration_totals == report.step_duration_totals
    assert restored.samples[0].step_durations == report.samples[0].step_durations
