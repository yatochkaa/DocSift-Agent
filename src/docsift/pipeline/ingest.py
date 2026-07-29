"""Асинхронный пайплайн приёма документа.

Сохраняет файл, заводит ``Document`` и запускает фоновую обработку:
извлечение текста → LLM-экстракция → guardrails → ``ReviewTask``.

Ответ на загрузку возвращается сразу со статусом ``uploaded``; тяжёлая работа
идёт в фоновой задаче, которая берёт свою сессию и обновляет статусы документа.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docsift.core.config import Settings, get_settings
from docsift.core.timing import (
    STEP_LLM_EXTRACTION,
    STEP_TEXT_EXTRACTION,
    StepTimings,
)
from docsift.db.models import Document, Extraction, ReviewTask
from docsift.domain.enums import DocumentStatus, ExtractionStatus, ReviewTaskStatus
from docsift.pipeline.storage import DocumentStorage, StoredFile
from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.guardrails import GuardrailResult
from docsift.services.guardrails import evaluate_guardrails
from docsift.services.llm import (
    LLMExtractionError,
    LLMExtractionService,
    LLMProviderError,
    build_llm_provider,
    build_pricing_table,
)
from docsift.services.llm.cache import DiskExtractionCache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from docsift.services.text_extraction import TextExtractionService

__all__ = [
    "DEFAULT_TENANT_ID",
    "ingest_document",
]

logger = logging.getLogger(__name__)

#: Тенант по умолчанию, если вызывающий слой не передал свой.
#: В многотенантной системе веб передаёт ``tenant_id`` из заголовка.
DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000000")

#: Ссылки на запущенные фоновые задачи, чтобы их не убил сборщик мусора.
_background_tasks: set[asyncio.Task[Any]] = set()

# Ленивые семафоры по event-loop и лимиту.  WeakKeyDictionary следит за тем,
# чтобы завершённые loop не утекали в память.
_semaphores_by_loop: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _get_semaphore(limit: int) -> asyncio.Semaphore:
    """Вернуть семафор для текущего event-loop с заданным *limit*.

    При первом обращении создаёт семафор и привязывает его к loop.
    Если *limit* изменился — создаёт новый семафор (старый больше не
    используется в данном loop).
    """
    loop = asyncio.get_running_loop()
    by_limit = _semaphores_by_loop.get(loop)
    if by_limit is None:
        by_limit = {}
        _semaphores_by_loop[loop] = by_limit
    sem = by_limit.get(limit)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        by_limit[limit] = sem
    return sem


def _basename(file_name: str) -> str:
    """Безопасное имя файла без пути, обрезанное до лимита колонки."""
    return Path(file_name.replace("\\", "/")).name[:512] or "document"


def _json_safe(value: Any) -> Any:
    """Привести значение к JSON-совместимому виду для JSONB-колонки."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Репозиторий экстракций, привязанный к сессии пайплайна
# ---------------------------------------------------------------------------
class _SessionExtractionRepository:
    """Обёртка над ``AsyncSession`` для ``LLMExtractionService``.

    В отличие от :class:`docsift.repositories.extractions.ExtractionRepository`,
    не делает ``commit`` — транзакцией управляет пайплайн. Заодно перехватывает
    последний созданный/обновлённый ``Extraction``, чтобы обогатить его
    таймингами шагов и стоимостью: сам сервис этого не делает, а веб читает
    эти поля из ``provider_settings``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        #: Последний Extraction, который сервис создал или обновил.
        #: ``None``, если был cache-hit или ответ пришёл из мока без записи в БД.
        self.captured: Extraction | None = None

    async def next_attempt_no(self, document_id: UUID) -> int:
        latest = await self._session.scalar(
            select(func.max(Extraction.attempt_no)).where(
                Extraction.document_id == document_id
            )
        )
        return (latest or 0) + 1

    async def create(self, extraction: Extraction) -> Extraction:
        self._session.add(extraction)
        await self._session.flush()
        self.captured = extraction
        return extraction

    async def update(self, extraction: Extraction) -> Extraction:
        await self._session.flush()
        self.captured = extraction
        return extraction


def _build_extraction_service(
    session: AsyncSession, settings: Settings
) -> tuple[Any, _SessionExtractionRepository]:
    """Собрать дефолтный ``LLMExtractionService`` с перехватывающим репозиторием.

    Возвращает кортеж ``(service, repository)``: сервис соответствует контракту
    ``extract(document_id, text_result) -> ExtractedDocument``, а из репозитория
    пайплайн заберёт созданную ``Extraction`` для обогащения.
    """
    provider = build_llm_provider(settings)
    repository = _SessionExtractionRepository(session)
    cache = DiskExtractionCache()
    service = LLMExtractionService(
        provider,
        repository,
        prompt_version=settings.llm_prompt_version,
        cache=cache,
    )
    return service, repository


def _cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    settings: Settings,
) -> Decimal | None:
    """Стоимость вызова LLM по таблице цен настроек."""
    return build_pricing_table(settings).cost_usd(model, input_tokens, output_tokens)


def _make_extraction(
    *,
    document_id: UUID,
    attempt_no: int,
    settings: Settings,
    status: ExtractionStatus,
    started_at: datetime | None,
) -> Extraction:
    """Создать ``Extraction`` вручную (cache-hit, мок или ошибка до LLM)."""
    return Extraction(
        document_id=document_id,
        attempt_no=attempt_no,
        status=status,
        schema_version="1",
        provider=settings.llm_provider,
        model=settings.llm_model,
        prompt_version=settings.llm_prompt_version,
        prompt_text="",
        provider_settings={},
        llm_attempts=[],
        started_at=started_at,
    )


def _persist_review_tasks(
    session: AsyncSession,
    extraction: Extraction,
    result: GuardrailResult,
) -> None:
    """Создать по одной ``ReviewTask`` на каждое нарушение guardrail."""
    for violation in result.violations:
        session.add(
            ReviewTask(
                extraction_id=extraction.id,
                field_path=violation.field_path[:512],
                reason=violation.message[:1000],
                status=ReviewTaskStatus.PENDING,
                original_value=_json_safe(violation.actual),
            )
        )


# ---------------------------------------------------------------------------
# Публичная точка входа
# ---------------------------------------------------------------------------
async def ingest_document(
    *,
    file_name: str,
    payload: bytes,
    session_factory: "async_sessionmaker[AsyncSession]",
    tenant_id: UUID | None = None,
    settings: Settings | None = None,
    storage: DocumentStorage | None = None,
    text_extraction_service: TextExtractionService | None = None,
    extraction_service: Any | None = None,
    background: bool = True,
) -> dict[str, Any]:
    """Принять документ к обработке.

    Сохраняет файл через :class:`DocumentStorage`, заводит ``Document`` и
    запускает фоновую обработку. Возвращает словарь с ключами ``id`` и
    ``status`` для строки прогресса и поллинга.

    При повторной загрузке того же содержимого возвращает уже существующий
    документ, не падая по уникальному индексу ``object_key``.

    Raises:
        UploadTooLargeError: размер файла превышает лимит.
        UnsupportedContentTypeError: тип файла не поддерживается.
    """
    resolved_settings = settings or get_settings()
    resolved_storage = storage or DocumentStorage.from_settings(resolved_settings)
    tenant = tenant_id or DEFAULT_TENANT_ID

    # 1. Сохранить файл на диск. Синхронно: выбрасывает ошибки лимита/типа
    # ещё до того, как мы коснёмся базы — запрос валиден или нет сразу.
    stored = resolved_storage.save(file_name=file_name, payload=payload)

    # 2. Найти существующий документ по object_key или создать новый.
    async with session_factory() as session:
        existing = await session.scalar(
            select(Document).where(Document.object_key == stored.object_key)
        )
        if existing is not None:
            return {
                "id": str(existing.id),
                "status": str(existing.status),
                "already_existed": True,
            }

        document = Document(
            tenant_id=tenant,
            original_filename=_basename(file_name),
            object_key=stored.object_key,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            status=DocumentStatus.UPLOADED,
        )
        session.add(document)
        await session.commit()
        await session.refresh(document)
        document_id = document.id

    # 3. Запустить фоновую обработку или выполнить синхронно (для тестов).
    if background:
        task = asyncio.create_task(
            _process_document(
                document_id=document_id,
                stored=stored,
                session_factory=session_factory,
                settings=resolved_settings,
                text_extraction_service=text_extraction_service,
                extraction_service=extraction_service,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"id": str(document_id), "status": DocumentStatus.UPLOADED.value}

    final_status = await _process_document(
        document_id=document_id,
        stored=stored,
        session_factory=session_factory,
        settings=resolved_settings,
        text_extraction_service=text_extraction_service,
        extraction_service=extraction_service,
    )
    return {"id": str(document_id), "status": final_status}


# ---------------------------------------------------------------------------
# Фоновая обработка одного документа
# ---------------------------------------------------------------------------
async def _process_document(
    *,
    document_id: UUID,
    stored: StoredFile,
    session_factory: "async_sessionmaker[AsyncSession]",
    settings: Settings,
    text_extraction_service: TextExtractionService | None,
    extraction_service: Any | None,
) -> str:
    """Обработать документ: текст → LLM → guardrails → статусы.

    Каждая фоновая задача берёт свою сессию; объекты между сессиями не
    переносятся — внутри ``_run_with_session`` мы работаем с «своими»
    экземплярами и айдишниками.

    Возвращает финальный статус документа (строкой).
    """
    semaphore = _get_semaphore(settings.llm_max_concurrency)
    try:
        async with semaphore:
            async with session_factory() as session:
                return await _run_with_session(
                    session=session,
                    document_id=document_id,
                    stored=stored,
                    settings=settings,
                    text_extraction_service=text_extraction_service,
                    extraction_service=extraction_service,
                )
    except Exception:
        # Финальная страховка: ни одна ошибка не должна «вывалиться» молча.
        # Сессия уже закрыта, поэтому просто фиксируем статус через новую.
        logger.exception("Непредвиденная ошибка обработки документа %s", document_id)
        await _mark_failed(document_id, session_factory)
        return DocumentStatus.FAILED.value


async def _run_with_session(
    *,
    session: AsyncSession,
    document_id: UUID,
    stored: StoredFile,
    settings: Settings,
    text_extraction_service: TextExtractionService | None,
    extraction_service: Any | None,
) -> str:
    """Основная логика обработки в рамках одной сессии/транзакции."""
    if text_extraction_service is None:
        # Ленивый импорт: на уровне модуля сервис импортируется только для
        # типов (TYPE_CHECKING), поэтому в рантайме его надо тянуть явно.
        from docsift.services.text_extraction import TextExtractionService

        text_service = TextExtractionService(
            pdf_max_pages=settings.pdf_max_pages,
            pdf_max_render_megapixels=settings.pdf_max_render_megapixels,
        )
    else:
        text_service = text_extraction_service

    timings = StepTimings()

    # Загружаем документ в этой сессии и переводим в processing.
    document = await session.get(Document, document_id)
    if document is None:
        logger.error("Документ %s не найден после создания", document_id)
        return DocumentStatus.FAILED.value
    document.status = DocumentStatus.PROCESSING
    await session.commit()

    repository = _SessionExtractionRepository(session)
    llm_service = extraction_service
    if llm_service is None:
        llm_service, repository = _build_extraction_service(session, settings)

    try:
        # Шаг 1: извлечение текста (синхронный I/O — в отдельном потоке).
        with timings.measure(STEP_TEXT_EXTRACTION):
            text_result = await asyncio.to_thread(
                text_service.extract, str(stored.absolute_path)
            )

        # Шаг 2: LLM-экстракция.
        with timings.measure(STEP_LLM_EXTRACTION):
            extracted = await llm_service.extract(document_id, text_result)

    except (LLMProviderError, LLMExtractionError) as exc:
        # Сервис уже записал ошибку в Extraction через репозиторий-обёртку.
        await _handle_extraction_failure(
            session=session,
            document=document,
            repository=repository,
            settings=settings,
            timings=timings,
            error_code=_error_code(exc),
            error_message=str(exc)[:2000],
        )
        return DocumentStatus.FAILED.value

    except Exception as exc:
        # Ошибка извлечения текста или иная — Extraction мог не создаться.
        await _handle_extraction_failure(
            session=session,
            document=document,
            repository=repository,
            settings=settings,
            timings=timings,
            error_code="text_extraction_failed",
            error_message=str(exc)[:2000],
        )
        return DocumentStatus.FAILED.value

    # Успешная экстракция: финализируем Extraction и guardrails.
    # captured is None говорит лишь о том, что сервис не писал Extraction через
    # наш репозиторий. Для чужого сервиса это всегда верно и ничего не значит.
    cache_hit = repository.captured is None and extraction_service is None
    extraction = await _finalize_success(
        session=session,
        repository=repository,
        document_id=document_id,
        settings=settings,
        extracted=extracted,
        timings=timings,
        cache_hit=cache_hit,
    )

    # Шаг 3: guardrails на извлечённом документе.
    guardrail_result = evaluate_guardrails(extracted, settings)
    extraction.requires_review = guardrail_result.requires_review

    # Определяем тип документа из результата.
    # SQLAlchemy Enum сопоставляет строку с именем члена (PAYMENT_INVOICE),
    # а не со значением (payment_invoice) — передаём сам член.
    document.detected_type = _unwrap(extracted.document_type)
    document.status = (
        DocumentStatus.REVIEW_REQUIRED
        if guardrail_result.requires_review
        else DocumentStatus.COMPLETED
    )

    # Шаг 4: создаём ReviewTask на каждое нарушение.
    _persist_review_tasks(session, extraction, guardrail_result)

    await session.commit()
    return document.status.value


def _unwrap(field_or_value: object) -> object:
    """Распаковать ``ExtractedField``, если он пришёл обёрнутым.

    Если ``field_or_value`` — экземпляр ``ExtractedField`` (или любого объекта
    с атрибутом ``value``), вернёт ``.value``.  Иначе — вернёт исходное
    значение.  Это безопасно для ``None`` и уже «развёрнутых» значений.
    """
    return getattr(field_or_value, "value", field_or_value)


def _error_code(exc: Exception) -> str:
    """Стабильный код ошибки для ``Extraction.error_code``."""
    if isinstance(exc, LLMProviderError):
        return "provider_error"
    if isinstance(exc, LLMExtractionError):
        return "schema_validation_failed"
    return type(exc).__name__


async def _finalize_success(
    *,
    session: AsyncSession,
    repository: _SessionExtractionRepository,
    document_id: UUID,
    settings: Settings,
    extracted: ExtractedDocument,
    timings: StepTimings,
    cache_hit: bool,
) -> Extraction:
    """Доработать ``Extraction`` после успешной экстракции.

    Если сервис сам создал ``Extraction`` (реальный LLM-вызов) — берём её и
    обогащаем. Если ответ пришёл из кеша или мока (``captured is None``) —
    создаём запись вручную, чтобы у документа всегда была экстракция.
    """
    extraction = repository.captured
    if extraction is None:
        attempt_no = await repository.next_attempt_no(document_id)
        extraction = _make_extraction(
            document_id=document_id,
            attempt_no=attempt_no,
            settings=settings,
            status=ExtractionStatus.SUCCEEDED,
            started_at=None,
        )
        extraction.result = extracted.model_dump(mode="json")
        extraction.completed_at = None
        session.add(extraction)
        await session.flush()

    # Обогащаем provider_settings таймингами, стоимостью и флагом кеша —
    # веб читает именно эти ключи.
    provider_settings = dict(extraction.provider_settings or {})
    provider_settings["step_durations"] = timings.as_dict()
    provider_settings["cache_hit"] = cache_hit
    cost = _cost_usd(
        extraction.model,
        extraction.input_tokens,
        extraction.output_tokens,
        settings,
    )
    # JSONB не умеет Decimal: без float() commit падает на сериализации.
    provider_settings["cost_usd"] = None if cost is None else float(cost)
    extraction.provider_settings = provider_settings
    return extraction


async def _handle_extraction_failure(
    *,
    session: AsyncSession,
    document: Document,
    repository: _SessionExtractionRepository,
    settings: Settings,
    timings: StepTimings,
    error_code: str,
    error_message: str,
) -> None:
    """Перевести документ в ``failed`` и зафиксировать ошибку в Extraction."""
    document.status = DocumentStatus.FAILED

    extraction = repository.captured
    if extraction is None:
        # Экстракция не успела создаться (например, упало извлечение текста).
        attempt_no = await repository.next_attempt_no(document.id)
        extraction = _make_extraction(
            document_id=document.id,
            attempt_no=attempt_no,
            settings=settings,
            status=ExtractionStatus.FAILED,
            started_at=None,
        )
        session.add(extraction)

    extraction.status = ExtractionStatus.FAILED
    extraction.error_code = error_code[:64]
    extraction.error_message = error_message[:2000]
    if extraction.completed_at is None:
        extraction.completed_at = datetime.now(UTC)

    # Тайминги шагов полезны даже при сбое: видно, где остановились.
    provider_settings = dict(extraction.provider_settings or {})
    provider_settings["step_durations"] = timings.as_dict()
    provider_settings["cache_hit"] = False
    extraction.provider_settings = provider_settings

    await session.commit()


async def _mark_failed(
    document_id: UUID,
    session_factory: "async_sessionmaker[AsyncSession]",
) -> None:
    """Последняя страховка: пометить документ ``failed`` в отдельной сессии."""
    try:
        async with session_factory() as session:
            document = await session.get(Document, document_id)
            if document is not None and document.status != DocumentStatus.FAILED:
                document.status = DocumentStatus.FAILED
                await session.commit()
    except Exception:
        logger.exception("Не удалось пометить документ %s как failed", document_id)