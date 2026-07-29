"""Презентер списка документов `/documents`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

from .. import filters
from .common import Chip, EmptyState, Pagination, build_pagination, status_chip

SORTABLE = ("file_name", "doc_type", "counterparty", "total_amount", "doc_date", "status", "uploaded_at")

COLUMNS = (
    ("file_name", "Файл", "left"),
    ("doc_type", "Тип", "left"),
    ("counterparty", "Контрагент", "left"),
    ("total_amount", "Сумма", "right"),
    ("doc_date", "Дата документа", "right"),
    ("status", "Статус", "left"),
    ("uploaded_at", "Загружен", "right"),
)


@dataclass(frozen=True)
class DocumentRow:
    id: str
    href: str
    thumbnail_url: str | None
    file_name: str
    doc_type: str
    counterparty: str
    amount_text: str
    date_text: str
    uploaded_text: str
    status_chip: Chip
    is_processing: bool


@dataclass(frozen=True)
class SortableColumn:
    key: str
    label: str
    align: str
    href: str
    active: bool
    direction: str
    aria_sort: str
    sortable: bool = True


@dataclass(frozen=True)
class FilterOption:
    value: str
    label: str
    selected: bool


@dataclass(frozen=True)
class DocumentsPage:
    title: str
    rows: tuple[DocumentRow, ...]
    row_ids: tuple[str, ...]
    columns: tuple[SortableColumn, ...]
    pagination: Pagination
    query: str
    type_options: tuple[FilterOption, ...]
    status_options: tuple[FilterOption, ...]
    empty: EmptyState | None
    has_active_filters: bool
    base_query: str


def build_documents(
    *,
    items: Sequence[Mapping[str, Any]],
    total: int,
    page: int,
    per_page: int,
    query: str = "",
    doc_type: str = "",
    status: str = "",
    sort: str = "uploaded_at",
    direction: str = "desc",
    available_types: Sequence[str] = (),
    available_statuses: Sequence[str] = (),
) -> DocumentsPage:
    sort = sort if sort in SORTABLE else "uploaded_at"
    direction = "asc" if direction == "asc" else "desc"

    rows = tuple(
        DocumentRow(
            id=str(item["id"]),
            href=f"/documents/{item['id']}",
            thumbnail_url=item.get("thumbnail_url"),
            file_name=str(item.get("file_name") or "Без имени"),
            doc_type=str(item.get("doc_type") or "—"),
            counterparty=str(item.get("counterparty") or "—"),
            amount_text=filters.money(item.get("total_amount"), item.get("currency") or "₽")
            if item.get("total_amount") is not None
            else "—",
            date_text=filters.ru_date(item.get("doc_date")),
            uploaded_text=filters.ru_datetime(item.get("uploaded_at")),
            status_chip=status_chip(item.get("status")),
            is_processing=item.get("status") in {"uploaded", "pending", "processing", "extracted", "running"},
        )
        for item in items
    )

    base_params = {"q": query, "type": doc_type, "status": status, "sort": sort, "dir": direction}
    columns = tuple(
        _column(key, label, align, sort, direction, base_params) for key, label, align in COLUMNS
    )

    has_filters = bool(query or doc_type or status)
    empty = None
    if not rows:
        queue_empty = {
            "review_required": ("Проверять сейчас нечего", "Все документы требуют не больше вашего внимания."),
            "processing": ("Сейчас ничего не обрабатывается", "Новые загрузки появятся здесь автоматически."),
            "completed": ("Готовых документов пока нет", "Завершённые документы появятся после успешной обработки."),
            "failed": ("В этой очереди ошибок нет", "Документы с ошибкой обработки появятся здесь."),
        }
        if status in queue_empty and not query and not doc_type:
            title, hint = queue_empty[status]
            empty = EmptyState("inbox", f"{title}. {hint}", "Показать все документы", "/documents")
        elif has_filters:
            empty = EmptyState("search-x", "По выбранным условиям документов нет", "Сбросить фильтры", "/documents")
        else:
            empty = EmptyState("file-plus-2", "Здесь пока нет документов", "Загрузить документ", "#upload")

    return DocumentsPage(
        title="Документы",
        rows=rows,
        row_ids=tuple(row.id for row in rows),
        columns=columns,
        pagination=build_pagination(page, per_page, total),
        query=query,
        type_options=_options(available_types, doc_type, "Все типы"),
        status_options=_options(available_statuses, status, "Все статусы", labelize=True),
        empty=empty,
        has_active_filters=has_filters,
        base_query=urlencode({k: v for k, v in base_params.items() if v}),
    )


def _column(
    key: str, label: str, align: str, sort: str, direction: str, params: Mapping[str, str]
) -> SortableColumn:
    active = key == sort
    next_direction = "asc" if not active or direction == "desc" else "desc"
    query = dict(params)
    query.update({"sort": key, "dir": next_direction, "page": "1"})
    href = "/documents?" + urlencode({k: v for k, v in query.items() if v})
    aria = "none" if not active else ("ascending" if direction == "asc" else "descending")
    return SortableColumn(key, label, align, href, active, direction if active else "", aria)


def _options(
    values: Sequence[str], selected: str, all_label: str, labelize: bool = False
) -> tuple[FilterOption, ...]:
    from ..tokens import status_label

    options = [FilterOption("", all_label, selected == "")]
    for value in values:
        label = status_label(value) if labelize else value
        options.append(FilterOption(value, label, value == selected))
    return tuple(options)
