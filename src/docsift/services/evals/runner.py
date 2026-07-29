from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from docsift.core.config import EvalProfileName, ExtractionStrategy
from docsift.core.timing import (
    STEP_LLM_EXTRACTION,
    STEP_METRICS,
    STEP_TEXT_EXTRACTION,
    StepTimings,
    merge_step_durations,
)
from docsift.db.models import EvalRun, Extraction
from docsift.services.llm.cache import ExtractionCacheProtocol
from docsift.domain.enums import EvalRunStatus
from docsift.schemas.evals import (
    EvalPricing,
    EvalRunReport,
    EvalSampleResult,
    EvalTokenUsage,
    EvaluationMetrics,
)
from docsift.schemas.text_extraction import TextExtractionResult
from docsift.services.evals.dataset import Dataset
from docsift.services.evals.metrics import evaluate_document, merge_metrics
from docsift.services.llm import LLMExtractionService, LLMProviderProtocol


class EvalRunRepositoryProtocol(Protocol):
    async def create(self, eval_run: EvalRun) -> EvalRun: ...

    async def update(self, eval_run: EvalRun) -> EvalRun: ...


class TextExtractorProtocol(Protocol):
    def extract(self, source_path: str | Path) -> TextExtractionResult: ...


ProgressCallback = Callable[[int, int], None]


class InMemoryExtractionRepository:
    def __init__(self) -> None:
        self._extractions: dict[UUID, list[Extraction]] = {}

    async def next_attempt_no(self, document_id: UUID) -> int:
        return len(self._extractions.get(document_id, [])) + 1

    async def create(self, extraction: Extraction) -> Extraction:
        self._extractions.setdefault(extraction.document_id, []).append(extraction)
        return extraction

    async def update(self, extraction: Extraction) -> Extraction:
        return extraction

    def latest(self, document_id: UUID) -> Extraction | None:
        items = self._extractions.get(document_id, [])
        return items[-1] if items else None


def calculate_cost_usd(
    pricing: EvalPricing,
    input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    def component(tokens: int | None, price: Decimal) -> Decimal | None:
        if price == 0:
            return Decimal(0)
        if tokens is None:
            return None
        return Decimal(tokens) * price / Decimal(1_000_000)

    input_cost = component(input_tokens, pricing.input_price_per_million)
    output_cost = component(output_tokens, pricing.output_price_per_million)
    if input_cost is None or output_cost is None:
        return None
    return input_cost + output_cost


def _sum_optional(values: list[int | None]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_costs(values: list[Decimal | None]) -> Decimal | None:
    if any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), start=Decimal(0))


class EvalRunner:
    def __init__(
        self,
        *,
        provider: LLMProviderProtocol,
        provider_profile: EvalProfileName,
        pricing: EvalPricing,
        prompt_version: str,
        text_extractor: TextExtractorProtocol,
        run_repository: EvalRunRepositoryProtocol,
        progress_callback: ProgressCallback | None = None,
        strategy: ExtractionStrategy = "cheap_only",
        bypass_cache: bool = True,
        expensive_provider: LLMProviderProtocol | None = None,
        expensive_pricing: EvalPricing | None = None,
        confidence_threshold: float = 0.85,
        dump_raw: bool = False,
        cache: ExtractionCacheProtocol | None = None,
    ) -> None:
        self._provider = provider
        self._provider_profile = provider_profile
        self._pricing = pricing
        self._prompt_version = prompt_version
        self._text_extractor = text_extractor
        self._run_repository = run_repository
        self._progress_callback = progress_callback
        self._strategy = strategy
        self._bypass_cache = bypass_cache
        self._expensive_provider = expensive_provider
        self._expensive_pricing = expensive_pricing
        self._confidence_threshold = confidence_threshold
        self._dump_raw = dump_raw
        self._cache = cache

    async def run(self, dataset: Dataset, *, limit: int | None = None) -> EvalRunReport:
        if limit is not None and limit < 1:
            raise ValueError("Eval limit must be greater than zero")
        if self._strategy == "cascade" and self._expensive_provider is None:
            raise ValueError("Cascade strategy requires an expensive provider")
        if self._strategy == "cascade" and self._expensive_pricing is None:
            raise ValueError("Cascade strategy requires expensive pricing")

        samples = dataset.samples[:limit] if limit is not None else dataset.samples
        run_id = uuid4()
        started_at = datetime.now(UTC)
        started_counter = perf_counter()
        eval_run = EvalRun(
            id=run_id,
            status=EvalRunStatus.RUNNING,
            dataset_name=dataset.manifest.name,
            dataset_version=dataset.manifest.version,
            schema_version=dataset.manifest.schema_version,
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            prompt_version=self._prompt_version,
            sample_count=len(samples),
            run_config={
                "provider_profile": self._provider_profile,
                "provider_backend": self._provider.provider_name,
                "pricing": self._pricing.model_dump(mode="json"),
                "limit": limit,
                "temperature": 0,
                "strategy": self._strategy,
                "bypass_cache": self._bypass_cache,
            },
            started_at=started_at,
        )
        await self._run_repository.create(eval_run)

        try:
            audit_repository = InMemoryExtractionRepository()
            extraction_service = LLMExtractionService(
                self._provider,
                audit_repository,
                prompt_version=self._prompt_version,
                cache=self._cache,
            )
            cascade_service = None
            if self._strategy == "cascade":
                from docsift.services.llm.cascade import CascadeExtractionService

                cascade_service = CascadeExtractionService(
                    self._provider,
                    self._expensive_provider,  # type: ignore[arg-type]
                    audit_repository,
                    prompt_version=self._prompt_version,
                    confidence_threshold=self._confidence_threshold,
                    cache=self._cache,
                )

            aggregate_metrics = EvaluationMetrics()
            results: list[EvalSampleResult] = []
            step_totals: dict[str, float] = {}

            for index, sample in enumerate(samples, start=1):
                sample_started = perf_counter()
                timings = StepTimings()
                document_id = uuid5(
                    NAMESPACE_URL,
                    f"{dataset.manifest.name}:{dataset.manifest.version}:{sample.sample_id}",
                )
                try:
                    with timings.measure(STEP_TEXT_EXTRACTION):
                        text_result = await asyncio.to_thread(
                            self._text_extractor.extract,
                            sample.document_path,
                        )
                    with timings.measure(STEP_LLM_EXTRACTION):
                        if cascade_service is not None:
                            cascade_result = await cascade_service.extract(
                                document_id,
                                text_result,
                            )
                            actual = cascade_result.document
                        else:
                            actual = await extraction_service.extract(
                                document_id,
                                text_result,
                                bypass_cache=self._bypass_cache,
                            )
                    with timings.measure(STEP_METRICS):
                        sample_metrics = evaluate_document(
                            sample.expected,
                            actual,
                            name_similarity_threshold=dataset.manifest.name_similarity_threshold,
                            line_item_match_threshold=dataset.manifest.line_item_match_threshold,
                        )
                    merge_metrics(aggregate_metrics, sample_metrics)
                    audit = audit_repository.latest(document_id)
                    input_tokens = audit.input_tokens if audit is not None else 0
                    output_tokens = audit.output_tokens if audit is not None else 0
                    results.append(
                        EvalSampleResult(
                            sample_id=sample.sample_id,
                            status="succeeded",
                            duration_seconds=perf_counter() - sample_started,
                            step_durations=timings.as_dict(),
                            token_usage=EvalTokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            ),
                            cost_usd=calculate_cost_usd(
                                self._pricing,
                                input_tokens,
                                output_tokens,
                            ),
                            metrics=sample_metrics,
                            raw_extracted=(
                                actual.model_dump(mode="json") if self._dump_raw else None
                            ),
                            raw_expected=(
                                sample.expected.model_dump(mode="json")
                                if self._dump_raw
                                else None
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - isolate each sample failure
                    audit = audit_repository.latest(document_id)
                    input_tokens = audit.input_tokens if audit is not None else 0
                    output_tokens = audit.output_tokens if audit is not None else 0
                    results.append(
                        EvalSampleResult(
                            sample_id=sample.sample_id,
                            status="failed",
                            duration_seconds=perf_counter() - sample_started,
                            step_durations=timings.as_dict(),
                            token_usage=EvalTokenUsage(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            ),
                            cost_usd=calculate_cost_usd(
                                self._pricing,
                                input_tokens,
                                output_tokens,
                            ),
                            error_type=type(exc).__name__,
                            error_message=str(exc)[:2000],
                            raw_expected=(
                                sample.expected.model_dump(mode="json")
                                if self._dump_raw
                                else None
                            ),
                        )
                    )
                finally:
                    merge_step_durations(step_totals, timings.as_dict())
                    if self._progress_callback is not None:
                        self._progress_callback(index, len(samples))

            completed_at = datetime.now(UTC)
            input_values = [item.token_usage.input_tokens for item in results]
            output_values = [item.token_usage.output_tokens for item in results]
            report = EvalRunReport(
                run_id=run_id,
                dataset_name=dataset.manifest.name,
                dataset_version=dataset.manifest.version,
                schema_version=dataset.manifest.schema_version,
                provider=self._provider_profile,
                provider_backend=self._provider.provider_name,
                model=self._provider.model_name,
                prompt_version=self._prompt_version,
                temperature=0,
                started_at=started_at,
                completed_at=completed_at,
                limit=limit,
                sample_count=len(samples),
                succeeded_count=sum(item.status == "succeeded" for item in results),
                failed_count=sum(item.status == "failed" for item in results),
                pricing=self._pricing,
                token_usage=EvalTokenUsage(
                    input_tokens=_sum_optional(input_values),
                    output_tokens=_sum_optional(output_values),
                ),
                cost_usd=_sum_costs([item.cost_usd for item in results]),
                total_duration_seconds=perf_counter() - started_counter,
                average_duration_seconds=(
                    sum(item.duration_seconds for item in results) / len(results)
                    if results
                    else 0
                ),
                step_duration_totals=step_totals,
                metrics=aggregate_metrics,
                samples=results,
            )
            eval_run.status = EvalRunStatus.COMPLETED
            eval_run.completed_at = completed_at
            eval_run.metrics = report.model_dump(mode="json")
            await self._run_repository.update(eval_run)
            return report
        except Exception as exc:
            eval_run.status = EvalRunStatus.FAILED
            eval_run.completed_at = datetime.now(UTC)
            eval_run.error_message = str(exc)[:2000]
            await self._run_repository.update(eval_run)
            raise

