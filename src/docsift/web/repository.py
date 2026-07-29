"""Асинхронный слой чтения для веб-интерфейса (только SELECT).

Адаптирован под реальные модели проекта:
``documents`` / ``extractions`` / ``eval_runs`` / ``review_tasks``.

Принципы:

* новых таблиц не заводим: guardrails собираются из ``review_tasks``,
  а все метрики прогона лежат в ``EvalRun.metrics`` — туда runner кладёт
  целиком сериализованный ``EvalRunReport``;
* значения полей документа лежат в ``Extraction.result`` (сериализованный
  ``ExtractedDocument``), каждое поле — ``{"value": ..., "confidence": ...}``;
* наружу отдаются простые словари — презентеры про ORM не знают.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from docsift.core.config import Settings
from docsift.db.models import Document, EvalRun, Extraction, ReviewTask
from docsift.domain.enums import DocumentStatus, ReviewTaskStatus
from docsift.schemas.documents import ExtractedDocument
from docsift.services.guardrails import evaluate_guardrails

# --- Сортировка -----------------------------------------------------------
# Сортировать можно только по настоящим колонкам documents. Контрагент, сумма
# и дата документа живут внутри JSONB extractions.result, поэтому для них
# сортировка не поддержана и молча падает на "загружен, по убыванию".
SORT_COLUMNS = {
    "file_name": Document.original_filename,
    "doc_type": Document.detected_type,
    "status": Document.status,
    "uploaded_at": Document.created_at,
}

PROCESSING_STATUSES = {"uploaded", "processing"}

# --- Человеческие подписи --------------------------------------------------
FIELD_LABELS = {
    "document_type": "Тип документа",
    "number": "Номер",
    "date": "Дата документа",
    "total_amount": "Итого",
    "vat_amount": "НДС",
    "currency": "Валюта",
    "supplier/name": "Поставщик",
    "supplier/inn": "ИНН поставщика",
    "supplier/kpp": "КПП поставщика",
    "buyer/name": "Покупатель",
    "buyer/inn": "ИНН покупателя",
    "buyer/kpp": "КПП покупателя",
    "payment_due_date": "Срок оплаты",
    "correction_number": "Номер исправления",
    "correction_date": "Дата исправления",
    "upd_status": "Статус УПД",
    "operation_name": "Содержание операции",
    "shipment_date": "Дата отгрузки",
    "basis_document": "Основание",
    "contract_number": "Номер договора",
    "contract_date": "Дата договора",
    "service_period_start": "Период услуг, начало",
    "service_period_end": "Период услуг, конец",
}

GUARDRAIL_LABELS = {
    "total_mismatch": "Итог не сходится с позициями",
    "vat_rate_mismatch": "НДС не сходится со ставкой",
    "invalid_inn": "ИНН не проходит проверку",
    "same_parties": "Поставщик и покупатель совпадают",
    "document_date_out_of_range": "Дата документа вне диапазона",
    "low_confidence": "Низкая уверенность модели",
}

DOCUMENT_TYPE_LABELS = {
    "payment_invoice": "Счёт на оплату",
    "vat_invoice": "Счёт-фактура",
    "universal_transfer_document": "УПД",
    "consignment_note_torg12": "ТОРГ-12",
    "work_completion_act": "Акт выполненных работ",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value) if hasattr(value, "value") else str(value)


def _enum_text(value: Any) -> str | None:
    """StrEnum -> строка, None остаётся None."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _unwrap(raw: Any) -> Any:
    """ExtractedField -> голое значение."""
    if isinstance(raw, Mapping) and "value" in raw:
        return raw.get("value")
    return raw


def _confidence(raw: Any) -> float | None:
    if isinstance(raw, Mapping):
        value = raw.get("confidence")
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _sources(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        found = raw.get("sources")
        if isinstance(found, Sequence) and not isinstance(found, (str, bytes)):
            return [dict(item) for item in found if isinstance(item, Mapping)]
    return []


def _is_field(raw: Any) -> bool:
    return isinstance(raw, Mapping) and "value" in raw


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_date(value: Any) -> Date | None:
    if value is None or isinstance(value, Date):
        return value
    try:
        return Date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _result(extraction: Any) -> dict[str, Any]:
    if extraction is None:
        return {}
    return dict(getattr(extraction, "result", None) or {})


# --- Документы -------------------------------------------------------------
def _latest_extraction_join(stmt: Select) -> Select:
    """Прицепить к documents последнюю по attempt_no попытку извлечения."""
    latest = (
        select(
            Extraction.document_id.label("document_id"),
            func.max(Extraction.attempt_no).label("attempt_no"),
        )
        .group_by(Extraction.document_id)
        .subquery()
    )
    return stmt.join(
        latest, latest.c.document_id == Document.id, isouter=True
    ).join(
        Extraction,
        (Extraction.document_id == latest.c.document_id)
        & (Extraction.attempt_no == latest.c.attempt_no),
        isouter=True,
    )


def _documents_query(*, query: str, doc_type: str, status: str) -> Select:
    stmt: Select = _latest_extraction_join(select(Document, Extraction))
    if query:
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                Document.original_filename.ilike(pattern),
                Extraction.result["supplier"]["name"]["value"].astext.ilike(pattern),
                Extraction.result["buyer"]["name"]["value"].astext.ilike(pattern),
                Extraction.result["number"]["value"].astext.ilike(pattern),
            )
        )
    if doc_type:
        stmt = stmt.where(Document.detected_type == doc_type)
    if status:
        stmt = stmt.where(Document.status == status)
    return stmt


async def list_documents(
    session: AsyncSession,
    *,
    query: str = "",
    doc_type: str = "",
    status: str = "",
    sort: str = "uploaded_at",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Страница списка документов + общее число строк."""
    stmt = _documents_query(query=query, doc_type=doc_type, status=status)

    count_stmt = select(func.count()).select_from(
        stmt.with_only_columns(Document.id).order_by(None).subquery()
    )
    total = int((await session.execute(count_stmt)).scalar_one() or 0)

    column = SORT_COLUMNS.get(sort, Document.created_at)
    stmt = stmt.order_by(column.asc() if direction == "asc" else column.desc())
    stmt = stmt.offset(max(0, (page - 1) * per_page)).limit(per_page)

    rows = (await session.execute(stmt)).all()
    return [_document_dict(doc, extraction) for doc, extraction in rows], total


async def document_facets(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Значения для фильтров «тип» и «статус»."""
    types = (await session.execute(select(Document.detected_type).distinct())).scalars().all()
    statuses = (await session.execute(select(Document.status).distinct())).scalars().all()
    return (
        sorted({_enum_text(t) for t in types if t is not None}),
        sorted({_enum_text(s) for s in statuses if s is not None}),
    )


async def latest_extraction(session: AsyncSession, document_id: UUID) -> Any | None:
    stmt = (
        select(Extraction)
        .where(Extraction.document_id == document_id)
        .order_by(Extraction.attempt_no.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def get_document_detail(session: AsyncSession, document_id: str) -> dict[str, Any] | None:
    """Готовый словарь карточки документа: сам документ, поля, guardrails, тайминги."""
    doc_uuid = _uuid(document_id)
    if doc_uuid is None:
        return None
    document = await session.get(Document, doc_uuid)
    if document is None:
        return None

    extraction = await latest_extraction(session, doc_uuid)
    tasks: Sequence[Any] = ()
    if extraction is not None:
        tasks = (
            (
                await session.execute(
                    select(ReviewTask)
                    .where(ReviewTask.extraction_id == extraction.id)
                    .order_by(ReviewTask.created_at)
                )
            )
            .scalars()
            .all()
        )

    raw_result = _result(extraction)
    corrections = _correction_map(tasks)
    effective_result = _apply_corrections(raw_result, corrections)

    return {
        "document": _document_dict(document, extraction),
        "extracted": _extracted_dict(
            extraction,
            result_override=effective_result,
            original_result=raw_result,
            corrections=corrections,
        ),
        "guardrails": [_guardrail_dict(task) for task in tasks],
        "step_durations": _step_durations(extraction),
        "pages": [],
        "review": {
            "open_count": sum(
                1 for task in tasks if _enum_text(getattr(task, "status", None)) in {"pending", "in_progress"}
            ),
            "correction_count": len(corrections),
            "can_complete": extraction is not None and bool(raw_result),
            "can_export": _enum_text(document.status) == "completed",
        },
    }


def _json_pointer_parts(path: str) -> list[str | int]:
    parts: list[str | int] = []
    for part in path.strip("/").split("/"):
        if not part:
            continue
        if part.lstrip("-").isdigit():
            parts.append(int(part))
        else:
            parts.append(part)
    return parts


def _field_container(data: dict[str, Any], path: str) -> dict[str, Any]:
    current: Any = data
    for i, part in enumerate(_json_pointer_parts(path)):
        if isinstance(part, int):
            if part < 0:
                raise ValueError(f"Отрицательный индекс не допускается: {part} в позиции {i}")
            if not isinstance(current, list):
                raise ValueError(f"Ожидался список для индекса {part}, получен {type(current).__name__}")
            if part >= len(current):
                raise ValueError(f"Индекс {part} выходит за границы списка (длина: {len(current)})")
            current = current[part]
        else:
            if not isinstance(current, dict):
                raise ValueError(f"Ожидался словарь для ключа '{part}', получен {type(current).__name__}")
            if part not in current:
                raise ValueError(f"Поле '{part}' не найдено в позиции {i} пути")
            current = current[part]
    if not isinstance(current, dict) or "value" not in current:
        raise ValueError(f"Поле по пути '{path}' не является контейнером поля (нет ключа 'value')")
    return current


def _correction_map(tasks: Sequence[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for task in tasks:
        if _enum_text(getattr(task, "status", None)) != "resolved":
            continue
        if task.corrected_value is None and not str(task.reason or "").startswith(
            "manual_correction"
        ):
            continue
        result[str(task.field_path)] = task.corrected_value
    return result


def _apply_corrections(result: Mapping[str, Any], corrections: Mapping[str, Any]) -> dict[str, Any]:
    effective = deepcopy(dict(result))
    for path, value in corrections.items():
        try:
            field = _field_container(effective, path)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        field["value"] = value
        field["confidence"] = 1.0 if value not in (None, "") else 0.0
        if value not in (None, "") and not field.get("sources"):
            field["sources"] = [
                {"kind": "pdf_text", "page": 1, "text": "Подтверждено при ручной проверке"}
            ]
        elif value in (None, ""):
            field["sources"] = []
    return effective


def _coerce_correction(raw: str) -> Any:
    text = raw.strip()
    return None if not text else text


async def save_document_correction(
    session: AsyncSession,
    document_id: str,
    field_path: str,
    value: str,
) -> None:
    doc_uuid = _uuid(document_id)
    if doc_uuid is None:
        raise ValueError("Документ не найден")
    document = await session.get(Document, doc_uuid)
    extraction = await latest_extraction(session, doc_uuid)
    if document is None or extraction is None or not extraction.result:
        raise ValueError("В документе пока нет извлечённых полей")

    raw_result = _result(extraction)
    original_field = _field_container(raw_result, field_path)
    corrected_value = _coerce_correction(value)

    existing_tasks = (
        (
            await session.execute(
                select(ReviewTask)
                .where(
                    ReviewTask.extraction_id == extraction.id,
                    ReviewTask.field_path == field_path,
                )
                .order_by(ReviewTask.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    corrections = _correction_map(existing_tasks)
    corrections[field_path] = corrected_value
    candidate = _apply_corrections(raw_result, corrections)
    ExtractedDocument.model_validate(candidate)

    task = existing_tasks[0] if existing_tasks else ReviewTask(
        extraction_id=extraction.id,
        field_path=field_path,
        reason="manual_correction: исправлено при ручной проверке",
        original_value=original_field.get("value"),
    )
    if not existing_tasks:
        session.add(task)
    task.corrected_value = corrected_value
    task.reason = "manual_correction: исправлено при ручной проверке"
    task.status = ReviewTaskStatus.RESOLVED
    task.resolution_comment = "Исправлено в интерфейсе DocSift"
    task.resolved_at = _utcnow()
    document.status = DocumentStatus.REVIEW_REQUIRED
    await session.commit()


async def delete_document(session: AsyncSession, document_id: str) -> str:
    """Delete a terminal document and its cascaded extraction/review rows."""
    doc_uuid = _uuid(document_id)
    if doc_uuid is None:
        raise ValueError("Документ не найден")
    document = await session.get(Document, doc_uuid)
    if document is None:
        raise ValueError("Документ не найден")
    if document.status in {DocumentStatus.UPLOADED, DocumentStatus.PROCESSING}:
        raise ValueError("Дождитесь завершения обработки документа")
    object_key = str(document.object_key)
    await session.delete(document)
    await session.commit()
    return object_key


async def save_bulk_document_corrections(
    session: AsyncSession,
    document_id: str,
    corrections_raw: Mapping[str, str],
) -> None:
    """Validate all corrections first, then persist them in one transaction."""
    doc_uuid = _uuid(document_id)
    if doc_uuid is None:
        raise ValueError("Документ не найден")
    document = await session.get(Document, doc_uuid)
    extraction = await latest_extraction(session, doc_uuid)
    if document is None or extraction is None or not extraction.result:
        raise ValueError("В документе пока нет извлечённых полей")

    raw_result = _result(extraction)
    tasks = (
        (await session.execute(select(ReviewTask).where(ReviewTask.extraction_id == extraction.id)))
        .scalars().all()
    )
    corrections = _correction_map(tasks)
    prepared = {path: _coerce_correction(value) for path, value in corrections_raw.items()}
    for path in prepared:
        _field_container(raw_result, path)
    candidate = _apply_corrections(raw_result, {**corrections, **prepared})
    validated = ExtractedDocument.model_validate(candidate)

    latest_by_path = {}
    for task in tasks:
        latest_by_path.setdefault(str(task.field_path), task)
    for path, value in prepared.items():
        original = _field_container(raw_result, path).get("value")
        task = latest_by_path.get(path)
        if task is None:
            task = ReviewTask(extraction_id=extraction.id, field_path=path, original_value=original)
            session.add(task)
        task.corrected_value = value
        task.reason = "manual_correction: исправлено при ручной проверке"
        task.status = ReviewTaskStatus.RESOLVED
        task.resolution_comment = "Исправлено в интерфейсе DocSift"
        task.resolved_at = _utcnow()

    from docsift.core.config import get_settings
    guardrails = evaluate_guardrails(validated, get_settings())
    active_paths = {violation.field_path for violation in guardrails.violations}
    now = _utcnow()
    for task in tasks:
        if task.field_path not in active_paths and task.status in {
            ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS
        }:
            task.status = ReviewTaskStatus.RESOLVED
            task.resolution_comment = "Проверка пройдена после изменения данных"
            task.resolved_at = now
    for violation in guardrails.violations:
        pending = next((
            task for task in tasks
            if task.field_path == violation.field_path
            and task.status in {ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS}
        ), None)
        if pending is None:
            pending = ReviewTask(
                extraction_id=extraction.id,
                field_path=violation.field_path,
                original_value=violation.actual,
            )
            session.add(pending)
            tasks.append(pending)
        pending.reason = f"{violation.rule.value}: {violation.message}"
        pending.status = ReviewTaskStatus.PENDING
        pending.resolved_at = None
        pending.resolution_comment = None

    document.status = DocumentStatus.REVIEW_REQUIRED
    await session.commit()


async def complete_document_review(
    session: AsyncSession,
    document_id: str,
    settings: Settings,
    *,
    confirm_warnings: bool = False,
) -> dict[str, Any]:
    doc_uuid = _uuid(document_id)
    if doc_uuid is None:
        raise ValueError("Документ не найден")
    document = await session.get(Document, doc_uuid)
    if document is None:
        raise ValueError("Документ не найден")
    if document.status != DocumentStatus.REVIEW_REQUIRED:
        raise ValueError("Документ не находится в статусе ручной проверки")
    extraction = await latest_extraction(session, doc_uuid)
    if extraction is None or not extraction.result:
        raise ValueError("Документ ещё не готов к проверке")
    tasks = (
        (
            await session.execute(
                select(ReviewTask).where(ReviewTask.extraction_id == extraction.id)
            )
        )
        .scalars()
        .all()
    )
    candidate = _apply_corrections(_result(extraction), _correction_map(tasks))
    validated = ExtractedDocument.model_validate(candidate)
    guardrails = evaluate_guardrails(validated, settings)
    active_paths = {violation.field_path for violation in guardrails.violations}

    for task in tasks:
        if task.field_path not in active_paths and task.status in {
            ReviewTaskStatus.PENDING,
            ReviewTaskStatus.IN_PROGRESS,
        }:
            task.status = ReviewTaskStatus.RESOLVED
            task.resolved_at = _utcnow()

    for violation in guardrails.violations:
        if any(
            task.field_path == violation.field_path
            and task.status in {ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS}
            for task in tasks
        ):
            continue
        task = ReviewTask(
            extraction_id=extraction.id,
            field_path=violation.field_path,
            reason=f"{violation.rule.value}: {violation.message}",
            status=ReviewTaskStatus.PENDING,
            original_value=violation.actual,
        )
        session.add(task)
        tasks.append(task)

    # Guardrails are review findings, not an unbreakable lock. The operator may
    # explicitly approve the effective data after comparing it with the source.
    violations = list(guardrails.violations)
    if violations and not confirm_warnings:
        await session.commit()
        return {
            "completed": False,
            "requires_confirmation": True,
            "has_warnings": True,
            "has_blocking": any(v.blocking for v in violations),
            "issues": len(violations),
        }

    document.status = DocumentStatus.COMPLETED
    confirmed = bool(confirm_warnings and violations)
    if confirmed:
        confirmed_paths = {violation.field_path for violation in violations}
        resolved_at = _utcnow()
        for task in tasks:
            if task.field_path in confirmed_paths and task.status in {
                ReviewTaskStatus.PENDING,
                ReviewTaskStatus.IN_PROGRESS,
            }:
                task.status = ReviewTaskStatus.RESOLVED
                task.resolution_comment = "Проверено и подтверждено пользователем"
                task.resolved_at = resolved_at
        session.add(
            ReviewTask(
                extraction_id=extraction.id,
                field_path="/review_completion",
                reason="manual_confirmation: Данные сверены с исходным документом",
                status=ReviewTaskStatus.RESOLVED,
                original_value="issues_confirmed",
                resolution_comment=f"Пользователь подтвердил {len(violations)} расхождений",
                resolved_at=resolved_at,
            )
        )

    extraction.requires_review = False
    await session.commit()
    return {
        "completed": True,
        "requires_confirmation": False,
        "has_warnings": bool(violations),
        "has_blocking": False,
        "issues": len(violations),
        "warnings_confirmed": confirmed,
    }


async def dashboard_data(session: AsyncSession, days: int = 30) -> dict[str, Any]:
    """Агрегаты для дашборда за текущий и предыдущий периоды."""
    now = _utcnow()
    current_from = now - timedelta(days=days)
    previous_from = now - timedelta(days=days * 2)

    async def count_documents(since: datetime, until: datetime) -> int:
        stmt = select(func.count(Document.id)).where(
            Document.created_at >= since, Document.created_at < until
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    documents_current = await count_documents(current_from, now)
    documents_previous = await count_documents(previous_from, current_from)

    runs = (
        (await session.execute(select(EvalRun).order_by(EvalRun.started_at.desc()).limit(20)))
        .scalars()
        .all()
    )
    run_dicts = [_run_dict(run) for run in runs][::-1]

    daily = (
        await session.execute(
            select(func.date(Document.created_at), func.count(Document.id))
            .where(Document.created_at >= current_from)
            .group_by(func.date(Document.created_at))
            .order_by(func.date(Document.created_at))
        )
    ).all()

    step_totals: dict[str, float] = defaultdict(float)
    for run in run_dicts:
        for step, value in (run.get("step_duration_totals") or {}).items():
            step_totals[step] += float(value or 0)

    status_rows = (
        await session.execute(
            select(Document.status, func.count(Document.id)).group_by(Document.status)
        )
    ).all()
    status_counts = {str(_enum_text(status) or ""): int(count or 0) for status, count in status_rows}

    recent_stmt = _latest_extraction_join(select(Document, Extraction))
    recent_rows = (
        await session.execute(recent_stmt.order_by(Document.created_at.desc()).limit(5))
    ).all()

    return {
        "documents_current": documents_current,
        "documents_previous": documents_previous,
        "documents_trend": [float(count) for _, count in daily],
        "runs": run_dicts,
        "step_duration_totals": dict(step_totals),
        "status_counts": status_counts,
        "recent_documents": [_document_dict(doc, extraction) for doc, extraction in recent_rows],
    }


async def recent_events(session: AsyncSession, limit: int = 12) -> list[dict[str, Any]]:
    """Лента событий: загрузки, прогоны, незакрытые проверки."""
    documents = (
        (await session.execute(select(Document).order_by(Document.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    runs = (
        (await session.execute(select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    review_rows = (
        await session.execute(
            select(ReviewTask, Extraction.document_id)
            .join(Extraction, Extraction.id == ReviewTask.extraction_id)
            .where(ReviewTask.status == "pending")
            .order_by(ReviewTask.created_at.desc())
            .limit(limit)
        )
    ).all()

    events: list[dict[str, Any]] = []
    for doc in documents:
        events.append(
            {
                "kind": "document",
                "title": doc.original_filename,
                "subtitle": DOCUMENT_TYPE_LABELS.get(
                    _enum_text(doc.detected_type) or "", "без типа"
                ),
                "created_at": doc.created_at,
                "href": f"/documents/{doc.id}",
                "chip": _enum_text(doc.status),
            }
        )
    for run in runs:
        events.append(
            {
                "kind": "run",
                "title": f"Прогон {run.id}",
                "subtitle": f"{(run.run_config or {}).get('strategy') or ''} · {run.model or ''}".strip(" ·"),
                "created_at": run.completed_at or run.started_at,
                "href": f"/evals/{run.id}",
                "chip": None,
            }
        )
    for task, document_id in review_rows:
        events.append(
            {
                "kind": "guardrail",
                "title": _guardrail_title(task),
                "subtitle": task.reason or "правило не пройдено",
                "created_at": task.created_at,
                "href": f"/documents/{document_id}",
                "chip": "Не пройдено",
                "tone": "danger",
            }
        )

    events.sort(
        key=lambda item: item["created_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return events[:limit]


# --- Прогоны evals ---------------------------------------------------------
async def list_runs(
    session: AsyncSession, *, page: int = 1, per_page: int = 20
) -> tuple[list[dict[str, Any]], int]:
    total = int((await session.execute(select(func.count(EvalRun.id)))).scalar_one() or 0)
    stmt = (
        select(EvalRun)
        .order_by(EvalRun.started_at.desc())
        .offset(max(0, (page - 1) * per_page))
        .limit(per_page)
    )
    runs = (await session.execute(stmt)).scalars().all()
    return [_run_dict(run) for run in runs], total


async def get_run(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    run_uuid = _uuid(run_id)
    if run_uuid is None:
        return None
    run = await session.get(EvalRun, run_uuid)
    if run is None:
        return None
    data = _run_dict(run)
    data["samples"] = _samples(run)
    return data


async def get_run_pair(
    session: AsyncSession, a: str, b: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return await get_run(session, a), await get_run(session, b)


# --- Маппинги в простые словари -------------------------------------------
def _document_dict(doc: Any, extraction: Any = None) -> dict[str, Any]:
    result = _result(extraction)
    supplier = result.get("supplier") or {}
    doc_type = _unwrap(result.get("document_type")) or _enum_text(doc.detected_type)
    return {
        "id": str(doc.id),
        "file_name": doc.original_filename,
        "doc_type": DOCUMENT_TYPE_LABELS.get(str(doc_type), doc_type),
        "counterparty": _unwrap((supplier or {}).get("name")),
        "total_amount": _decimal(_unwrap(result.get("total_amount"))),
        "currency": _unwrap(result.get("currency")),
        "doc_date": _as_date(_unwrap(result.get("date"))),
        "status": _enum_text(doc.status),
        "uploaded_at": doc.created_at,
        "thumbnail_url": None,
        # Размер и тип нужны карточке загрузки: после перезагрузки страницы
        # браузер их уже не помнит, а показать «PDF · 1,2 МБ» надо.
        "size_bytes": getattr(doc, "size_bytes", None),
        "content_type": getattr(doc, "content_type", None),
        "object_key": getattr(doc, "object_key", None),
        "source_url": f"/documents/{doc.id}/source",
        # Только код: Extraction.error_message — это str(exc), там попадается
        # сырой ответ провайдера. В интерфейс идёт текст по коду, см. tokens.py.
        "error_code": getattr(extraction, "error_code", None),
    }


def _extracted_dict(
    extraction: Any,
    *,
    result_override: Mapping[str, Any] | None = None,
    original_result: Mapping[str, Any] | None = None,
    corrections: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Поля, позиции и мета LLM для карточки документа."""
    if extraction is None:
        return {}
    result = dict(result_override) if result_override is not None else _result(extraction)
    original = dict(original_result) if original_result is not None else result
    correction_map = dict(corrections or {})
    supplier = result.get("supplier") or {}
    settings = dict(getattr(extraction, "provider_settings", None) or {})
    attempts = list(getattr(extraction, "llm_attempts", None) or [])
    cache_hit = bool(
        settings.get("cache_hit")
        or any(isinstance(a, Mapping) and a.get("cache_hit") for a in attempts)
    )
    input_tokens = getattr(extraction, "input_tokens", None) or 0
    output_tokens = getattr(extraction, "output_tokens", None) or 0
    total_tokens = (input_tokens + output_tokens) or None

    doc_type = _unwrap(result.get("document_type"))
    return {
        "doc_type": DOCUMENT_TYPE_LABELS.get(str(doc_type), doc_type),
        "counterparty": _unwrap((supplier or {}).get("name")),
        "total_amount": _decimal(_unwrap(result.get("total_amount"))),
        "currency": _unwrap(result.get("currency")),
        "doc_date": _as_date(_unwrap(result.get("date"))),
        "fields": _fields(result, original_result=original, corrections=correction_map),
        "line_items": _line_items(result.get("line_items") or []),
        "provider": getattr(extraction, "provider", None),
        "model": getattr(extraction, "model", None),
        "prompt_version": getattr(extraction, "prompt_version", None),
        "total_tokens": total_tokens,
        "cost": settings.get("cost_usd"),
        "cache_hit": cache_hit,
        "overall_confidence": (
            float(extraction.overall_confidence)
            if getattr(extraction, "overall_confidence", None) is not None
            else None
        ),
    }


def _fields(
    result: Mapping[str, Any],
    prefix: str = "",
    *,
    original_result: Mapping[str, Any] | None = None,
    corrections: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Плоский список полей ExtractedDocument (включая supplier/buyer)."""
    rows: list[dict[str, Any]] = []
    for key, raw in result.items():
        if key == "line_items":
            continue
        path = f"{prefix}/{key}" if prefix else key
        if _is_field(raw):
            pointer = "/" + path.strip("/")
            original_raw = (
                original_result.get(key)
                if isinstance(original_result, Mapping)
                else raw
            )
            rows.append(
                {
                    "path": pointer,
                    "name": FIELD_LABELS.get(path, path),
                    "value": _unwrap(raw),
                    "original_value": _unwrap(original_raw),
                    "corrected": pointer in (corrections or {}),
                    "confidence": _confidence(raw),
                    "sources": _sources(raw),
                }
            )
        elif isinstance(raw, Mapping):
            original_child = (
                original_result.get(key)
                if isinstance(original_result, Mapping) and isinstance(original_result.get(key), Mapping)
                else raw
            )
            rows.extend(
                _fields(
                    raw,
                    path,
                    original_result=original_child,
                    corrections=corrections,
                )
            )
    return rows


def _line_items(items: Sequence[Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        flat.append({key: _unwrap(value) for key, value in item.items()})
    return flat


def _step_durations(extraction: Any) -> dict[str, float]:
    """Тайминги шагов. В боевом пайплайне их пишут в provider_settings;
    если их нет — показываем хотя бы длительность вызова LLM."""
    if extraction is None:
        return {}
    settings = dict(getattr(extraction, "provider_settings", None) or {})
    durations = settings.get("step_durations")
    if isinstance(durations, Mapping) and durations:
        return {str(k): float(v or 0) for k, v in durations.items()}
    response_ms = getattr(extraction, "response_time_ms", None)
    if response_ms:
        return {"llm_extraction": float(response_ms) / 1000.0}
    return {}


def _guardrail_title(task: Any) -> str:
    reason = str(getattr(task, "reason", "") or "")
    for code, label in GUARDRAIL_LABELS.items():
        if reason.startswith(code) or code in reason:
            return label
    return str(getattr(task, "field_path", "") or "—")


def _guardrail_dict(task: Any) -> dict[str, Any]:
    status = _enum_text(getattr(task, "status", None))
    return {
        "rule": _guardrail_title(task),
        "passed": status in {"resolved", "rejected"},
        "message": f"{getattr(task, 'field_path', '')}: {getattr(task, 'reason', '')}".strip(": "),
    }


def _field_metrics(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """EvaluationMetrics.fields -> строки таблицы метрик (precision/recall/f1)."""
    rows: list[dict[str, Any]] = []
    for name, raw in (metrics.get("fields") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        precision = _ratio(raw.get("precision"), raw, ("matches",), ("matches", "hallucinations", "mismatches"))
        recall = _ratio(raw.get("recall"), raw, ("matches",), ("matches", "misses", "mismatches"))
        f1 = None
        if precision is not None and recall is not None and (precision + recall) > 0:
            f1 = 2 * precision * recall / (precision + recall)
        rows.append({"field": name, "precision": precision, "recall": recall, "f1": f1})
    return rows


def _ratio(
    serialized: Any,
    raw: Mapping[str, Any],
    numerator_keys: Sequence[str],
    denominator_keys: Sequence[str],
) -> float | None:
    if serialized is not None:
        try:
            return float(serialized)
        except (TypeError, ValueError):
            pass
    numerator = sum(int(raw.get(key) or 0) for key in numerator_keys)
    denominator = sum(int(raw.get(key) or 0) for key in denominator_keys)
    return numerator / denominator if denominator else None


def _overall_accuracy(metrics: Mapping[str, Any]) -> float | None:
    matches = misses = mismatches = 0
    for raw in (metrics.get("fields") or {}).values():
        if not isinstance(raw, Mapping):
            continue
        matches += int(raw.get("matches") or 0)
        misses += int(raw.get("misses") or 0)
        mismatches += int(raw.get("mismatches") or 0)
    denominator = matches + misses + mismatches
    return matches / denominator if denominator else None


def _errors(run: Any, report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if getattr(run, "error_message", None):
        errors.append(str(run.error_message))
    for sample in report.get("samples") or []:
        if isinstance(sample, Mapping) and sample.get("status") == "failed":
            errors.append(
                f"{sample.get('sample_id')}: {sample.get('error_type') or ''} "
                f"{sample.get('error_message') or ''}".strip()
            )
    return errors


def _samples(run: Any) -> list[dict[str, Any]]:
    report = dict(getattr(run, "metrics", None) or {})
    rows: list[dict[str, Any]] = []
    for sample in report.get("samples") or []:
        if not isinstance(sample, Mapping):
            continue
        rows.append(
            {
                "document_id": sample.get("sample_id"),
                "file_name": sample.get("sample_id"),
                "status": sample.get("status"),
                "duration_seconds": sample.get("duration_seconds"),
                "accuracy": _overall_accuracy(sample.get("metrics") or {}),
            }
        )
    return rows


def _run_dict(run: Any) -> dict[str, Any]:
    report = dict(getattr(run, "metrics", None) or {})
    config = dict(getattr(run, "run_config", None) or {})
    metrics = report.get("metrics") or {}
    return {
        "run_id": str(run.id),
        "started_at": run.started_at,
        "completed_at": getattr(run, "completed_at", None),
        "dataset": run.dataset_name,
        "dataset_version": run.dataset_version,
        "strategy": config.get("strategy"),
        "provider": report.get("provider_backend") or run.provider,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "documents": run.sample_count,
        "accuracy": _overall_accuracy(metrics),
        "duration_seconds": report.get("total_duration_seconds"),
        "cost": report.get("cost_usd"),
        "status": _enum_text(run.status),
        "step_duration_totals": report.get("step_duration_totals") or {},
        "metrics": _field_metrics(metrics),
        "errors": _errors(run, report),
    }
