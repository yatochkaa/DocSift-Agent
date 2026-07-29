from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from docsift.schemas.common import ExtractedField
from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.guardrails import GuardrailResult, GuardrailViolation
from docsift.schemas.llm import LLMMessage, LLMRequest
from docsift.schemas.text_extraction import TextExtractionResult
from docsift.services.llm.cache import ExtractionCacheProtocol
from docsift.services.llm.providers import LLMProviderProtocol
from docsift.services.llm.service import (
    ExtractionRepositoryProtocol,
    LLMExtractionService,
    serialize_text_extraction,
)

if TYPE_CHECKING:
    from docsift.core.config import Settings


def _build_field_map(document: ExtractedDocument) -> dict[str, ExtractedField[Any]]:
    """Отобразить каждый ``ExtractedField`` на его путь.

    Поля без значения (None) тоже включаем — expensive-модель может их заполнить.
    """
    found: dict[str, ExtractedField[Any]] = {}

    def walk(value: Any, path: str) -> None:
        if isinstance(value, ExtractedField):
            found[path.lstrip("/")] = value
            return
        if hasattr(type(value), "model_fields"):
            for name in type(value).model_fields:
                walk(getattr(value, name), f"{path}/{name}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}/{index}")

    walk(document, "")
    return found


@dataclass(slots=True)
class FieldProvenance:
    """Какое поле пришло от какой модели."""

    cheap_model: str
    expensive_model: str | None
    cheap_fields: set[str] = field(default_factory=set)
    expensive_fields: set[str] = field(default_factory=set)
    re_asked_paths: list[str] = field(default_factory=list)

    def source_of(self, field_path: str) -> str | None:
        """Вернуть имя модели, давшей конкретный путь поля."""
        if field_path in self.expensive_fields:
            return self.expensive_model
        if field_path in self.cheap_fields:
            return self.cheap_model
        return None


@dataclass(slots=True)
class CascadeResult:
    """Итог каскада: документ, результат guardrails, провенанс и стоимость."""

    document: ExtractedDocument
    guardrail_result: GuardrailResult
    provenance: FieldProvenance
    expensive_input_tokens: int | None = None
    expensive_output_tokens: int | None = None
    used_expensive_model: bool = False


class CascadeExtractionService:
    """Каскад моделей: дешёвая → guardrails → дорогая для спорных полей.

    Стратегия ``cascade``:
    1. Запрос к дешёвой/локальной модели через ``LLMExtractionService``.
    2. Результат прогоняется через существующий ``evaluate_guardrails``.
    3. Если нарушений нет и уверенность выше порога — принимаем, дорогую не трогаем.
    4. Если нарушения есть — повторный запрос к дорогой модели только по полям из
       ``field_path`` нарушений, затем значения сливаются обратно.
    """

    def __init__(
        self,
        cheap_provider: LLMProviderProtocol,
        expensive_provider: LLMProviderProtocol,
        repository: ExtractionRepositoryProtocol,
        *,
        prompt_version: str = "v1",
        confidence_threshold: float = 0.85,
        guardrail_evaluator: Callable[..., GuardrailResult] | None = None,
        cache: ExtractionCacheProtocol | None = None,
    ) -> None:
        self._cheap_service = LLMExtractionService(
            cheap_provider,
            repository,
            prompt_version=prompt_version,
            cache=cache,
        )
        self._expensive_provider = expensive_provider
        self._cheap_model = cheap_provider.model_name
        self._expensive_model = expensive_provider.model_name
        self._confidence_threshold = confidence_threshold
        self._guardrail_evaluator = guardrail_evaluator

    @property
    def cheap_service(self) -> LLMExtractionService:
        return self._cheap_service

    async def extract(
        self,
        document_id: UUID,
        text_result: TextExtractionResult,
        *,
        settings: Settings | None = None,
    ) -> CascadeResult:
        cheap_document = await self._cheap_service.extract(
            document_id, text_result, bypass_cache=False
        )

        guardrail_result = self._evaluate(cheap_document, settings)

        provenance = FieldProvenance(
            cheap_model=self._cheap_model,
            expensive_model=self._expensive_model,
        )
        provenance.cheap_fields = set(_build_field_map(cheap_document).keys())

        if not guardrail_result.requires_review:
            return CascadeResult(
                document=cheap_document,
                guardrail_result=guardrail_result,
                provenance=provenance,
            )

        disputed_paths = self._disputed_paths(guardrail_result.violations, cheap_document)
        if not disputed_paths:
            return CascadeResult(
                document=cheap_document,
                guardrail_result=guardrail_result,
                provenance=provenance,
            )

        merged, expensive_tokens = await self._re_ask_expensive(
            cheap_document, text_result, disputed_paths
        )
        provenance.expensive_fields = set(disputed_paths)
        provenance.re_asked_paths = list(disputed_paths)

        return CascadeResult(
            document=merged,
            guardrail_result=guardrail_result,
            provenance=provenance,
            expensive_input_tokens=expensive_tokens[0],
            expensive_output_tokens=expensive_tokens[1],
            used_expensive_model=True,
        )

    def _evaluate(self, document: ExtractedDocument, settings: Settings | None) -> GuardrailResult:
        if self._guardrail_evaluator is not None:
            return self._guardrail_evaluator(document)
        if settings is None:
            from docsift.core.config import get_settings

            settings = get_settings()
        from docsift.services.guardrails import evaluate_guardrails

        return evaluate_guardrails(document, settings)

    def _disputed_paths(
        self, violations: list[GuardrailViolation], document: ExtractedDocument
    ) -> list[str]:
        """Пути полей для переспрашивания на основе ``field_path`` из нарушений."""
        field_map = _build_field_map(document)
        candidate_paths: list[str] = []
        for violation in violations:
            path = violation.field_path
            if path in field_map:
                candidate_paths.append(path)
            else:
                prefix = path.rstrip("/") + "/"
                candidate_paths.extend(leaf for leaf in field_map if leaf.startswith(prefix))
        seen: set[str] = set()
        unique: list[str] = []
        for path in candidate_paths:
            if path not in seen:
                seen.add(path)
                unique.append(path)
        return unique

    async def _re_ask_expensive(
        self,
        cheap_document: ExtractedDocument,
        text_result: TextExtractionResult,
        disputed_paths: list[str],
    ) -> tuple[ExtractedDocument, tuple[int | None, int | None]]:
        field_map = _build_field_map(cheap_document)
        requested_fields = {
            path: field_map[path].model_dump(mode="json")
            for path in disputed_paths
            if path in field_map
        }

        system_prompt = (
            "Ты — корректор извлечения. Дешёвая модель уже извлекла документ, "
            "но часть полей помечена как спорные. Перепроверь ТОЛЬКО указанные поля "
            "по исходному тексту и верни для них исправленные значения в формате "
            "JSON-объекта: ключ — путь поля, значение — объект {value, confidence, sources}. "
            "Не выдумывай значения: если данных нет, верни value=null, confidence=0, sources=[]."
        )
        user_prompt = (
            "Исходный текст документа:\n"
            + serialize_text_extraction(text_result)
            + "\n\nСпорные поля для перепроверки (путь → текущее значение):\n"
            + json.dumps(requested_fields, ensure_ascii=False, indent=2)
        )
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ]

        response = await self._expensive_provider.complete(
            LLMRequest(messages=tuple(messages), json_schema={})
        )
        corrections = self._parse_corrections(response.content)
        merged = self._apply_corrections(cheap_document, corrections)
        return merged, (response.input_tokens, response.output_tokens)

    @staticmethod
    def _parse_corrections(content: str) -> dict[str, dict[str, Any]]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in parsed.items():
            if isinstance(value, dict) and {"value", "confidence", "sources"} <= value.keys():
                result[key] = value
        return result

    @staticmethod
    def _apply_corrections(
        document: ExtractedDocument, corrections: dict[str, dict[str, Any]]
    ) -> ExtractedDocument:
        if not corrections:
            return document
        raw = document.model_dump(mode="json")

        def set_path(data: Any, path: str, value: dict[str, Any]) -> None:
            parts = [int(p) if p.isdigit() else p for p in path.strip("/").split("/") if p]
            current = data
            for part in parts[:-1]:
                current = current[part]
            current[parts[-1]] = value

        for path, value in corrections.items():
            try:
                set_path(raw, path, value)
            except (KeyError, IndexError, TypeError):
                continue

        return ExtractedDocument.model_validate(raw)

