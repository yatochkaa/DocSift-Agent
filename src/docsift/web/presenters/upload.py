"""Презентер карточки загрузки — единственное место, где живут её состояния.

Карточку рисуют два роута: POST /documents/upload сразу после приёма файла и
GET /partials/uploads/{id} на каждом тике поллинга. Оба зовут
:func:`build_upload_card`, поэтому карточка не может «похудеть» по дороге:
после первого же опроса пользователь видит ту же структуру, а не голый чип.

Шаблон логики не содержит: он обходит готовые поля.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .. import filters
from ..tokens import (
    UPLOAD_STAGE_NAMES,
    ToneName,
    is_upload_pending,
    status_tone,
    upload_error_text,
    upload_headline,
    upload_stage_index,
    upload_status_label,
)

# Расширение говорит пользователю больше, чем MIME-тип.
CONTENT_TYPE_LABELS: dict[str, str] = {
    "application/pdf": "PDF",
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/tiff": "TIFF",
}

# Статусы, после которых обработка уже не сдвинется.
TERMINAL_STATUSES = frozenset({"review_required", "completed", "succeeded", "failed"})


@dataclass(frozen=True)
class UploadStage:
    """Один видимый этап обработки."""

    label: str
    state: str  # done | active | pending | failed
    number: int

    @property
    def is_done(self) -> bool:
        return self.state == "done"

    @property
    def is_active(self) -> bool:
        return self.state == "active"

    @property
    def is_failed(self) -> bool:
        return self.state == "failed"


@dataclass(frozen=True)
class UploadCard:
    """Постоянная карточка последней загрузки."""

    document_id: str | None
    file_name: str
    meta_text: str
    uploaded_at_text: str
    status: str
    status_label: str
    status_tone: ToneName
    headline: str
    detail: str
    stages: tuple[UploadStage, ...]
    polling: bool
    poll_url: str | None
    href: str | None
    action_label: str | None
    note: str | None
    error_text: str | None
    is_duplicate: bool
    is_error: bool
    is_terminal: bool


def file_size_text(size_bytes: Any) -> str:
    """1536 -> '1,5 КБ'. Для неизвестного размера — прочерк."""
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "—"
    if size < 0:
        return "—"
    if size < 1024:
        return f"{int(size)} Б"
    for unit, digits in (("КБ", 0), ("МБ", 1), ("ГБ", 2)):
        size /= 1024
        if size < 1024 or unit == "ГБ":
            return f"{filters.number(size, digits)} {unit}"
    return "—"


def _type_text(content_type: str | None, file_name: str) -> str:
    """Понятное имя формата: сначала MIME, иначе расширение файла."""
    label = CONTENT_TYPE_LABELS.get((content_type or "").strip().lower())
    if label:
        return label
    _, _, suffix = file_name.rpartition(".")
    return suffix.upper() if suffix and suffix != file_name else "Файл"


def _stages(*, stage_index: int, is_error: bool, has_document: bool, last_label: str) -> tuple[UploadStage, ...]:
    """Три этапа с состоянием каждого.

    Ошибка приёма файла (лимит размера, чужой формат) случается до того, как
    документ заведён, — тогда падает первый же этап, а не последний.
    """
    failed_at = 1 if (is_error and not has_document) else (3 if is_error else 0)
    names = (UPLOAD_STAGE_NAMES[0], UPLOAD_STAGE_NAMES[1], last_label)
    stages: list[UploadStage] = []
    for position, name in enumerate(names, start=1):
        if position == failed_at:
            state = "failed"
        elif failed_at and position > failed_at:
            state = "pending"
        elif position < stage_index:
            state = "done"
        elif position == stage_index:
            state = "done" if stage_index == 3 else "active"
        else:
            state = "pending"
        stages.append(UploadStage(label=name, state=state, number=position))
    return tuple(stages)


def build_upload_card(
    *,
    document_id: str | None,
    file_name: str,
    status: str | None,
    size_bytes: Any = None,
    content_type: str | None = None,
    uploaded_at: datetime | None = None,
    already_existed: bool = False,
    error_code: str | None = None,
) -> UploadCard:
    """Собрать карточку по данным приёма файла или очередного опроса статуса."""
    key = (status or "").strip() or ("failed" if error_code else "uploaded")
    is_error = key == "failed" or bool(error_code)
    is_terminal = key in TERMINAL_STATUSES
    # Дубликат — не ошибка и не обработка: документ уже есть, повторно его
    # никто не считает. Поллинг для него включаем только если он всё ещё в работе.
    polling = is_upload_pending(key) and bool(document_id) and not is_error

    href = f"/documents/{document_id}/review" if document_id else None
    if already_existed and is_error:
        headline = "Документ уже есть, предыдущая обработка не завершилась"
        detail = "Это не новая ошибка загрузки. Откройте сохранённый документ, чтобы увидеть причину."
        action_label = "Открыть ошибку"
    elif already_existed:
        headline = "Этот документ уже был загружен"
        detail = "Повторная обработка не запускалась — открывается ранее сохранённый документ."
        action_label = "Открыть документ"
    elif is_error:
        headline = "Не удалось обработать документ"
        detail = upload_error_text(error_code)
        action_label = "Открыть подробности" if document_id else None
    elif key == "review_required":
        headline = "Документ готов к проверке"
        detail = "Часть полей требует подтверждения человеком."
        action_label = "Перейти к проверке"
    elif key in ("completed", "succeeded"):
        headline = "Документ успешно обработан"
        detail = "Все поля извлечены и прошли проверки."
        action_label = "Открыть документ"
    else:
        headline = upload_headline(key)
        detail = "Обработка идёт в фоне — страницу можно не держать открытой."
        action_label = None

    last_stage = upload_status_label(key) if is_terminal else UPLOAD_STAGE_NAMES[2]
    meta_parts = [_type_text(content_type, file_name)]
    if size_bytes is not None:
        meta_parts.append(file_size_text(size_bytes))

    return UploadCard(
        document_id=document_id,
        file_name=file_name or "документ",
        meta_text=" · ".join(meta_parts),
        uploaded_at_text=filters.ru_datetime(uploaded_at) if uploaded_at else "только что",
        status=key,
        status_label=upload_status_label(key),
        status_tone=status_tone(key),
        headline=headline,
        detail=detail,
        stages=_stages(
            stage_index=upload_stage_index(key),
            is_error=is_error,
            has_document=bool(document_id),
            last_label=last_stage,
        ),
        polling=polling,
        poll_url=f"/partials/uploads/{document_id}" if polling else None,
        href=href,
        action_label=action_label,
        note="Документ уже есть в архиве" if already_existed else None,
        error_text=detail if is_error else None,
        is_duplicate=already_existed,
        is_error=is_error,
        is_terminal=is_terminal or already_existed,
    )


def card_from_document(
    document: Mapping[str, Any],
    *,
    error_code: str | None = None,
) -> UploadCard:
    """Карточка по строке документа из шлюза — используется поллингом."""
    return build_upload_card(
        document_id=str(document.get("id") or "") or None,
        file_name=str(document.get("file_name") or "документ"),
        status=document.get("status"),
        size_bytes=document.get("size_bytes"),
        content_type=document.get("content_type"),
        uploaded_at=document.get("uploaded_at"),
        error_code=error_code,
    )
