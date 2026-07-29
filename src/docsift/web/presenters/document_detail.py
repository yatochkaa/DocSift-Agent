"""Презентер карточки документа `/documents/{id}`.

Используются только поля, которые есть в ExtractedDocument / GuardrailResult:
fields[] (name, value, confidence, sources[].page, sources[].bbox), line_items[],
guardrail results (rule, passed, message), тайминги шагов и мета LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .. import filters
from ..tokens import ToneName, confidence_tone
from .common import Chip, EmptyState, WaterfallRow, build_waterfall, status_chip


@dataclass(frozen=True)
class SourceBox:
    """bbox в долях страницы, готовый для inline-стиля подсветки."""

    page: int
    style: str


@dataclass(frozen=True)
class FieldRow:
    key: str
    path: str
    name: str
    value_text: str
    input_value: str
    original_value_text: str
    corrected: bool
    confidence: float | None
    confidence_text: str
    tone: ToneName
    sources: tuple[SourceBox, ...]
    has_source: bool


@dataclass(frozen=True)
class LineItemRow:
    index: int
    cells: tuple[str, ...]
    editable_cells: dict[str, Any]


@dataclass(frozen=True)
class GuardrailRow:
    rule: str
    passed: bool
    chip: Chip
    message: str


@dataclass(frozen=True)
class PagePreview:
    number: int
    image_url: str | None
    width: int
    height: int


@dataclass(frozen=True)
class TraceMeta:
    provider: str
    model: str
    prompt_version: str
    tokens_text: str
    cost_text: str
    cache_hit: bool
    cache_chip: Chip


@dataclass(frozen=True)
class DocumentDetailPage:
    title: str
    document_id: str
    file_name: str
    status_chip: Chip
    doc_type: str
    counterparty: str
    amount_text: str
    date_text: str
    source_url: str | None
    source_kind: str
    pages: tuple[PagePreview, ...]
    fields: tuple[FieldRow, ...]
    line_item_headers: tuple[str, ...]
    line_items: tuple[LineItemRow, ...]
    line_item_keys: tuple[str, ...]
    line_items_total_text: str
    guardrails: tuple[GuardrailRow, ...]
    guardrails_failed: int
    waterfall: tuple[WaterfallRow, ...]
    total_duration_text: str
    trace: TraceMeta
    raw_json: str
    fields_empty: EmptyState | None
    guardrails_empty: EmptyState | None
    open_review_count: int
    correction_count: int
    can_complete: bool
    can_export: bool


def build_document_detail(
    *,
    document: Mapping[str, Any],
    extracted: Mapping[str, Any],
    guardrails: Sequence[Mapping[str, Any]] = (),
    step_durations: Mapping[str, float] | None = None,
    pages: Sequence[Mapping[str, Any]] = (),
    review: Mapping[str, Any] | None = None,
) -> DocumentDetailPage:
    fields = tuple(_field_row(index, raw) for index, raw in enumerate(extracted.get("fields", []) or []))
    headers, items, total_text, keys = _line_items(extracted.get("line_items") or [])
    guard_rows = tuple(sorted((_guardrail_row(g) for g in guardrails), key=lambda row: row.passed))
    waterfall = tuple(build_waterfall((step_durations or {}).items()))
    total_seconds = sum((step_durations or {}).values())

    return DocumentDetailPage(
        title=str(document.get("file_name") or "Документ"),
        document_id=str(document.get("id")),
        file_name=str(document.get("file_name") or "Без имени"),
        status_chip=status_chip(document.get("status")),
        doc_type=str(extracted.get("doc_type") or document.get("doc_type") or "—"),
        counterparty=str(extracted.get("counterparty") or document.get("counterparty") or "—"),
        amount_text=filters.money(extracted.get("total_amount"), extracted.get("currency") or "₽")
        if extracted.get("total_amount") is not None
        else "—",
        date_text=filters.ru_date(extracted.get("doc_date") or document.get("doc_date")),
        source_url=str(document.get("source_url") or "") or None,
        source_kind="pdf" if str(document.get("content_type") or "").lower() == "application/pdf" else "image",
        pages=tuple(
            PagePreview(
                number=int(p.get("number", i + 1)),
                image_url=p.get("image_url"),
                width=int(p.get("width", 850)),
                height=int(p.get("height", 1100)),
            )
            for i, p in enumerate(pages)
        ),
        fields=fields,
        line_item_headers=headers,
        line_items=items,
        line_item_keys=keys,
        line_items_total_text=total_text,
        guardrails=guard_rows,
        guardrails_failed=sum(1 for row in guard_rows if not row.passed),
        waterfall=waterfall,
        total_duration_text=filters.duration(total_seconds) if step_durations else "—",
        trace=_trace(extracted),
        raw_json=json.dumps(_jsonable(extracted), ensure_ascii=False, indent=2, default=str),
        fields_empty=_build_fields_empty(fields, document.get("status")),
        guardrails_empty=None if guard_rows else EmptyState("shield-check", "Правила не запускались", None, None),
        open_review_count=int((review or {}).get("open_count") or 0),
        correction_count=int((review or {}).get("correction_count") or 0),
        can_complete=bool((review or {}).get("can_complete")),
        can_export=bool((review or {}).get("can_export")),
    )


def _field_row(index: int, raw: Mapping[str, Any]) -> FieldRow:
    confidence = raw.get("confidence")
    confidence = float(confidence) if confidence is not None else None
    sources = tuple(_source_box(s) for s in (raw.get("sources") or []) if s.get("bbox"))
    value = raw.get("value")
    return FieldRow(
        key=f"field-{index}",
        path=str(raw.get("path") or ""),
        name=str(raw.get("name") or "—"),
        value_text=_value_text(value),
        input_value="" if value is None else str(value),
        original_value_text=_value_text(raw.get("original_value")),
        corrected=bool(raw.get("corrected")),
        confidence=confidence,
        confidence_text=filters.percent(confidence) if confidence is not None else "—",
        tone=confidence_tone(confidence),
        sources=sources,
        has_source=bool(sources),
    )


def _bbox_corners(bbox: Any) -> list[float]:
    """Углы рамки из любой из двух форм записи.

    LLM и схема ``BoundingBox`` отдают объект ``{x1, y1, x2, y2}``, а часть
    источников и фикстуры — список ``[x0, y0, x1, y1]``. Раньше объект
    молча разбирался как последовательность ключей и падал на ``float('x1')``,
    из-за чего страница проверки отдавала 500 на реальных данных.
    """
    if isinstance(bbox, Mapping):
        values = [bbox.get("x1"), bbox.get("y1"), bbox.get("x2"), bbox.get("y2")]
    else:
        values = list(bbox or [])
    corners: list[float] = []
    for value in (values + [0, 0, 0, 0])[:4]:
        try:
            corners.append(float(value))
        except (TypeError, ValueError):
            corners.append(0.0)
    return corners


def _source_box(source: Mapping[str, Any]) -> SourceBox:
    """bbox в долях страницы → процентный стиль подсветки."""
    x0, y0, x1, y1 = _bbox_corners(source.get("bbox"))
    left, top = min(x0, x1) * 100, min(y0, y1) * 100
    width, height = abs(x1 - x0) * 100, abs(y1 - y0) * 100
    style = f"left:{left:.3f}%;top:{top:.3f}%;width:{width:.3f}%;height:{height:.3f}%;"
    return SourceBox(page=int(source.get("page", 1) or 1), style=style)


def _value_text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (int, float)):
        return filters.number(value, 0 if float(value).is_integer() else 2)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


_LINE_ITEM_LABELS = {
    "name": "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435",
    "title": "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435",
    "description": "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435",
    "code": "\u0410\u0440\u0442\u0438\u043a\u0443\u043b",
    "sku": "\u0410\u0440\u0442\u0438\u043a\u0443\u043b",
    "qty": "\u041a\u043e\u043b-\u0432\u043e",
    "quantity": "\u041a\u043e\u043b-\u0432\u043e",
    "unit": "\u0415\u0434. \u0438\u0437\u043c.",
    "price": "\u0426\u0435\u043d\u0430",
    "unit_price": "\u0426\u0435\u043d\u0430 \u0437\u0430 \u0435\u0434.",
    "amount": "\u0421\u0443\u043c\u043c\u0430",
    "total": "\u0421\u0443\u043c\u043c\u0430",
    "sum": "\u0421\u0443\u043c\u043c\u0430",
    "discount": "\u0421\u043a\u0438\u0434\u043a\u0430",
    "vat": "\u041d\u0414\u0421",
    "vat_rate": "\u0421\u0442\u0430\u0432\u043a\u0430 \u041d\u0414\u0421",
    "vat_amount": "\u0421\u0443\u043c\u043c\u0430 \u041d\u0414\u0421",
    "tax": "\u041d\u0430\u043b\u043e\u0433",
    "currency": "\u0412\u0430\u043b\u044e\u0442\u0430",
}


def _build_fields_empty(fields: tuple[FieldRow, ...], document_status: str | None) -> EmptyState | None:
    if fields:
        return None
    status = str(document_status or "").lower()
    if status in ("uploaded", "processing"):
        return EmptyState("loader-2", "Извлечение данных ещё выполняется", "Проверить снова", "#review")
    elif status == "failed":
        return EmptyState("alert-circle", "Обработка документа завершилась ошибкой", None, None)
    else:
        return EmptyState("file-question", "Поля ещё не извлечены", None, None)


def _line_items(items: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, ...], tuple[LineItemRow, ...], str, tuple[str, ...]]:
    if not items:
        return (), (), "—", ()
    keys: list[str] = []
    for item in items:
        for key in item:
            if key not in keys:
                keys.append(key)
    rows: list[LineItemRow] = []
    for index, item in enumerate(items):
        cells = tuple(_value_text(item.get(key)) for key in keys)
        editable_cells: dict[str, dict[str, Any]] = {}
        for key in keys:
            cell_value = item.get(key)
            if cell_value is not None:
                editable_cells[key] = {
                    "path": f"/line_items/{index}/{key}",
                    "value": cell_value,
                    "input_value": "" if cell_value is None else str(cell_value),
                    "corrected": False,
                    "original_value": _value_text(cell_value),
                }
        rows.append(LineItemRow(index=index, cells=cells, editable_cells=editable_cells))
    total = sum(float(item.get("amount") or item.get("total") or 0) for item in items)
    headers = tuple(_LINE_ITEM_LABELS.get(key, key) for key in keys)
    return headers, rows, filters.money(total), tuple(keys)


def _guardrail_row(raw: Mapping[str, Any]) -> GuardrailRow:
    passed = bool(raw.get("passed"))
    return GuardrailRow(
        rule=str(raw.get("rule") or raw.get("name") or "—"),
        passed=passed,
        chip=Chip("Пройдено" if passed else "Не пройдено", "success" if passed else "danger"),
        message=str(raw.get("message") or ""),
    )


def _trace(extracted: Mapping[str, Any]) -> TraceMeta:
    cache_hit = bool(extracted.get("cache_hit"))
    return TraceMeta(
        provider=str(extracted.get("provider") or "—"),
        model=str(extracted.get("model") or "—"),
        prompt_version=str(extracted.get("prompt_version") or "—"),
        tokens_text=filters.tokens(extracted.get("total_tokens")),
        cost_text=filters.usd(extracted.get("cost")),
        cache_hit=cache_hit,
        cache_chip=Chip("Кеш" if cache_hit else "Без кеша", "success" if cache_hit else "muted"),
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
