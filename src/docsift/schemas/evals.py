from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, computed_field, model_validator

from docsift.domain.enums import DocumentType
from docsift.schemas.common import SchemaModel


class ExpectedParty(SchemaModel):
    name: str | None = None
    inn: str | None = None
    kpp: str | None = None


class ExpectedLineItem(SchemaModel):
    name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    vat_rate: Decimal | str | None = None
    vat_amount: Decimal | None = None


class ExpectedDocument(SchemaModel):
    document_type: DocumentType | None = None
    number: str | None = None
    date: Date | None = None
    supplier: ExpectedParty = Field(default_factory=ExpectedParty)
    buyer: ExpectedParty = Field(default_factory=ExpectedParty)
    total_amount: Decimal | None = None
    vat_amount: Decimal | None = None
    currency: str | None = None
    line_items: list[ExpectedLineItem] = Field(default_factory=list)


class DatasetManifest(SchemaModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(default="1", min_length=1, max_length=32)
    name_similarity_threshold: float = Field(default=0.85, ge=0, le=1)
    line_item_match_threshold: float = Field(default=0.45, ge=0, le=1)


class FieldMetrics(SchemaModel):
    matches: int = Field(default=0, ge=0)
    misses: int = Field(default=0, ge=0)
    hallucinations: int = Field(default=0, ge=0)
    mismatches: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def ignore_serialized_computed_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        cleaned = value.copy()
        cleaned.pop("accuracy", None)
        cleaned.pop("precision", None)
        cleaned.pop("recall", None)
        return cleaned

    @computed_field
    @property
    def accuracy(self) -> float | None:
        denominator = self.matches + self.misses + self.mismatches
        return self.matches / denominator if denominator else None

    @computed_field
    @property
    def precision(self) -> float | None:
        denominator = self.matches + self.hallucinations + self.mismatches
        return self.matches / denominator if denominator else None

    @computed_field
    @property
    def recall(self) -> float | None:
        denominator = self.matches + self.misses + self.mismatches
        return self.matches / denominator if denominator else None


class EvaluationMetrics(SchemaModel):
    fields: dict[str, FieldMetrics] = Field(default_factory=dict)


class EvalPricing(SchemaModel):
    currency: Literal["USD"] = "USD"
    input_price_per_million: Decimal = Field(ge=0)
    output_price_per_million: Decimal = Field(ge=0)


class EvalTokenUsage(SchemaModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class EvalSampleResult(SchemaModel):
    sample_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]
    duration_seconds: float = Field(ge=0)
    # Длительности шагов конвейера. У упавшего документа здесь остаются
    # только те шаги, до которых он успел дойти, включая аварийный.
    step_durations: dict[str, float] = Field(default_factory=dict)
    token_usage: EvalTokenUsage = Field(default_factory=EvalTokenUsage)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    metrics: EvaluationMetrics | None = None
    error_type: str | None = None
    error_message: str | None = None
    raw_extracted: dict[str, Any] | None = None
    raw_expected: dict[str, Any] | None = None


class EvalRunReport(SchemaModel):
    report_version: str = "3"
    run_id: UUID
    dataset_name: str
    dataset_version: str
    schema_version: str
    provider: Literal["local", "cloud"]
    provider_backend: str
    model: str
    prompt_version: str
    temperature: Literal[0] = 0
    started_at: datetime
    completed_at: datetime
    limit: int | None = Field(default=None, ge=1)
    sample_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    pricing: EvalPricing
    token_usage: EvalTokenUsage
    cost_usd: Decimal | None = Field(default=None, ge=0)
    total_duration_seconds: float = Field(ge=0)
    average_duration_seconds: float = Field(default=0, ge=0)
    step_duration_totals: dict[str, float] = Field(default_factory=dict)
    metrics: EvaluationMetrics
    samples: list[EvalSampleResult]


class EvalFieldComparison(SchemaModel):
    field: str
    status: Literal["improved", "regressed", "unchanged", "mixed"]
    run_a: FieldMetrics
    run_b: FieldMetrics
    delta_matches: int
    delta_misses: int
    delta_hallucinations: int
    delta_mismatches: int
    accuracy_a: float | None = None
    accuracy_b: float | None = None
    delta_accuracy: float | None = None
    precision_a: float | None = None
    precision_b: float | None = None
    delta_precision: float | None = None
    recall_a: float | None = None
    recall_b: float | None = None
    delta_recall: float | None = None


class EvalRunComparison(SchemaModel):
    dataset_name: str
    dataset_version: str
    schema_version: str
    run_a_id: UUID
    run_b_id: UUID
    provider_a: str
    provider_b: str
    model_a: str
    model_b: str
    prompt_version_a: str
    prompt_version_b: str
    cost_usd_a: Decimal | None = Field(default=None, ge=0)
    cost_usd_b: Decimal | None = Field(default=None, ge=0)
    total_duration_seconds_a: float = Field(ge=0)
    total_duration_seconds_b: float = Field(ge=0)
    fields: list[EvalFieldComparison]


