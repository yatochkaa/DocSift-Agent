from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from docsift.db.models import Extraction
from docsift.domain.enums import ExtractionStatus
from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.llm import LLMMessage, LLMRequest
from docsift.schemas.text_extraction import BoundingBox, TextExtractionResult
from docsift.services.llm.cache import ExtractionCacheProtocol
from docsift.services.llm.prompt import load_document_extraction_prompt
from docsift.services.llm.providers import LLMProviderError, LLMProviderProtocol
from docsift.services.llm.sources_restore import restore_sources

MAX_LLM_ATTEMPTS = 2
SCHEMA_VERSION = "1"


class ExtractionRepositoryProtocol(Protocol):
    async def next_attempt_no(self, document_id: UUID) -> int: ...

    async def create(self, extraction: Extraction) -> Extraction: ...

    async def update(self, extraction: Extraction) -> Extraction: ...


class LLMExtractionError(RuntimeError):
    def __init__(self, message: str, validation_errors: list[str]) -> None:
        super().__init__(message)
        self.validation_errors = validation_errors


def _excel_column(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _spreadsheet_cell(page_width: float, page_height: float, bbox: BoundingBox) -> str:
    column = max(1, round(bbox.x0 * page_width) + 1)
    row = max(1, round(bbox.y0 * page_height) + 1)
    return f"{_excel_column(column)}{row}"


def _page_source_ref(
    result: TextExtractionResult,
    page_number: int,
    page_label: str | None,
    page_width: float,
    page_height: float,
    used_ocr: bool,
    bbox: BoundingBox | None,
    text: str | None,
) -> dict[str, Any]:
    if result.media_type in {
        "text/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }:
        cell_range = (
            _spreadsheet_cell(page_width, page_height, bbox)
            if bbox is not None
            else f"A1:{_excel_column(max(1, round(page_width)))}{max(1, round(page_height))}"
        )
        return {
            "kind": "spreadsheet",
            "page": None,
            "bbox": None,
            "sheet": page_label or f"Sheet{page_number}",
            "cell_range": cell_range,
            "text": text,
        }

    kind = "ocr" if used_ocr else "pdf_text"
    if result.media_type.startswith("image/") and not used_ocr:
        kind = "image"
    return {
        "kind": kind,
        "page": page_number,
        "bbox": (
            {
                "x1": round(bbox.x0, 4),
                "y1": round(bbox.y0, 4),
                "x2": round(bbox.x1, 4),
                "y2": round(bbox.y1, 4),
            }
            if bbox is not None and bbox.x1 > bbox.x0 and bbox.y1 > bbox.y0
            else None
        ),
        "sheet": None,
        "cell_range": None,
        "text": text[:500] if text else None,
    }


def serialize_text_extraction(result: TextExtractionResult) -> str:
    pages: list[dict[str, Any]] = []
    for page in result.pages:
        blocks = [
            {
                "text": block.text,
                "source_ref": _page_source_ref(
                    result,
                    page.number,
                    page.label,
                    page.width,
                    page.height,
                    page.used_ocr,
                    block.bbox,
                    None,
                ),
            }
            for block in page.blocks
        ]
        tables = [
            {
                "rows": table.rows,
                "source_ref": _page_source_ref(
                    result,
                    page.number,
                    page.label,
                    page.width,
                    page.height,
                    page.used_ocr,
                    table.bbox,
                    None,
                ),
            }
            for table in page.tables
        ]
        pages.append(
            {
                "number": page.number,
                "label": page.label,
                "blocks": blocks,
                "tables": tables,
            }
        )
    return json.dumps({"media_type": result.media_type, "pages": pages}, ensure_ascii=False)


def _serialized_messages(messages: list[LLMMessage]) -> str:
    return json.dumps(
        [{"role": message.role, "content": message.content} for message in messages],
        ensure_ascii=False,
    )


def _confidence_summary(document: ExtractedDocument) -> tuple[Decimal | None, bool]:
    confidences: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if {"value", "confidence", "sources"}.issubset(value):
                confidences.append(float(value["confidence"]))
            else:
                for child in value.values():
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document.model_dump(mode="json"))
    if not confidences:
        return None, True
    average = Decimal(str(sum(confidences) / len(confidences))).quantize(Decimal("0.0001"))
    return average, any(confidence < 0.8 for confidence in confidences)


class LLMExtractionService:
    def __init__(
        self,
        provider: LLMProviderProtocol,
        repository: ExtractionRepositoryProtocol,
        prompt_version: str = "v1",
        cache: ExtractionCacheProtocol | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._prompt = load_document_extraction_prompt(prompt_version)
        self._cache = cache

    @property
    def cache(self) -> ExtractionCacheProtocol | None:
        return self._cache

    async def extract(
        self,
        document_id: UUID,
        text_result: TextExtractionResult,
        *,
        bypass_cache: bool = False,
    ) -> ExtractedDocument:
        cache_key = None
        if self._cache is not None and not bypass_cache:
            cache_key = self._cache.make_key(
                text_result,
                self._prompt.version,
                self._provider.model_name,
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached.document

        schema = ExtractedDocument.model_json_schema()
        system_prompt = self._prompt.text
        if not self._provider.supports_json_schema:
            system_prompt += (
                "\n\nПровайдер не поддерживает нативную JSON Schema. "
                "Верни только JSON, соответствующий этой схеме:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(
                role="user",
                content="Извлеки документ из этих данных:\n" + serialize_text_extraction(text_result),
            ),
        ]
        extraction = Extraction(
            document_id=document_id,
            attempt_no=await self._repository.next_attempt_no(document_id),
            status=ExtractionStatus.RUNNING,
            schema_version=SCHEMA_VERSION,
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            prompt_version=self._prompt.version,
            prompt_text=_serialized_messages(messages),
            provider_settings={
                "native_structured_output": self._provider.supports_json_schema,
                "max_attempts": MAX_LLM_ATTEMPTS,
            },
            llm_attempts=[],
            started_at=datetime.now(UTC),
        )
        await self._repository.create(extraction)

        input_tokens = 0
        output_tokens = 0
        input_tokens_known = False
        output_tokens_known = False
        response_time_ms = 0
        validation_errors: list[str] = []

        for attempt_number in range(1, MAX_LLM_ATTEMPTS + 1):
            started = perf_counter()
            try:
                response = await self._provider.complete(
                    LLMRequest(messages=tuple(messages), json_schema=schema)
                )
            except LLMProviderError as exc:
                response_time_ms += round((perf_counter() - started) * 1000)
                error_text = str(exc)
                extraction.llm_attempts = [
                    *extraction.llm_attempts,
                    {
                        "attempt": attempt_number,
                        "provider_error": error_text,
                    },
                ]
                extraction.status = ExtractionStatus.FAILED
                extraction.error_code = "provider_error"
                extraction.error_message = error_text[:2000]
                extraction.completed_at = datetime.now(UTC)
                extraction.prompt_text = _serialized_messages(messages)
                extraction.input_tokens = input_tokens if input_tokens_known else None
                extraction.output_tokens = output_tokens if output_tokens_known else None
                extraction.response_time_ms = response_time_ms
                await self._repository.update(extraction)
                raise
            response_time_ms += round((perf_counter() - started) * 1000)
            extraction.raw_response = response.content
            if response.input_tokens is not None:
                input_tokens += response.input_tokens
                input_tokens_known = True
            if response.output_tokens is not None:
                output_tokens += response.output_tokens
                output_tokens_known = True

            attempt_audit: dict[str, Any] = {
                "attempt": attempt_number,
                "raw_response": response.content,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
            try:
                payload = restore_sources(json.loads(response.content), text_result)
                document = ExtractedDocument.model_validate(payload)
            except (ValidationError, json.JSONDecodeError) as exc:
                error_text = str(exc)
                validation_errors.append(error_text)
                attempt_audit["validation_error"] = error_text
                extraction.llm_attempts = [*extraction.llm_attempts, attempt_audit]
                if attempt_number < MAX_LLM_ATTEMPTS:
                    messages.extend(
                        [
                            LLMMessage(role="assistant", content=response.content),
                            LLMMessage(
                                role="user",
                                content=(
                                    "Ответ не прошёл Pydantic-валидацию. Исправь JSON, "
                                    "не добавляя неподтверждённые значения. Ошибки:\n"
                                    + error_text
                                ),
                            ),
                        ]
                    )
                    continue

                raw = response.content
                opens, closes = raw.count("{"), raw.count("}")
                brace_hint = f"unmatched +{opens - closes}" if opens > closes else "balanced"
                num_predict = getattr(self._provider, "_num_predict", None)
                diag = (
                    f"model={self._provider.model_name}"
                    f" | num_predict={num_predict}"
                    f" | response_len={len(raw)}"
                    f" | json_braces={brace_hint}"
                    f" | validation_error={error_text[:500]}"
                    f" | raw_preview={raw[:800]}"
                )

                extraction.status = ExtractionStatus.FAILED
                extraction.error_code = "schema_validation_failed"
                extraction.error_message = diag[:2000]
                extraction.completed_at = datetime.now(UTC)
                extraction.prompt_text = _serialized_messages(messages)
                extraction.input_tokens = input_tokens if input_tokens_known else None
                extraction.output_tokens = output_tokens if output_tokens_known else None
                extraction.response_time_ms = response_time_ms
                await self._repository.update(extraction)
                raise LLMExtractionError(diag, validation_errors) from exc

            extraction.llm_attempts = [*extraction.llm_attempts, attempt_audit]
            extraction.status = ExtractionStatus.SUCCEEDED
            extraction.result = document.model_dump(mode="json")
            extraction.overall_confidence, extraction.requires_review = _confidence_summary(document)
            extraction.completed_at = datetime.now(UTC)
            extraction.prompt_text = _serialized_messages(messages)
            extraction.input_tokens = input_tokens if input_tokens_known else None
            extraction.output_tokens = output_tokens if output_tokens_known else None
            extraction.response_time_ms = response_time_ms
            await self._repository.update(extraction)
            if cache_key is not None:
                self._cache.store(cache_key, document)
            return document

        raise AssertionError("Unreachable LLM extraction state")

