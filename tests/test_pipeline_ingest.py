"""Тесты пайплайна приёма документа (``ingest_document``).

Покрывают: успешный путь, повторную загрузку, превышение лимита, неподдерживаемый
тип, отказ LLM-провайдера и создание ``ReviewTask`` при сработавшем guardrail.

База эмулируется in-memory фейковой сессией — как ``FakeExtractionRepository``
в ``test_llm_extraction.py``. Реальная СУБД не нужна.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from docsift.core.config import Settings
from docsift.db.models import Document, Extraction, ReviewTask
from docsift.domain.enums import (
    DocumentStatus,
    DocumentType,
    ExtractionStatus,
    ReviewTaskStatus,
)
from docsift.pipeline import ingest_document
from docsift.pipeline.storage import (
    DocumentStorage,
    UnsupportedContentTypeError,
    UploadTooLargeError,
)
from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.llm import LLMProviderError


# ---------------------------------------------------------------------------
# In-memory «БД» с фейковой асинхронной сессией
# ---------------------------------------------------------------------------
class FakeDatabase:
    """Разделяемое in-memory хранилище документов/экстракций/проверок.

    Каждая ``FakeAsyncSession`` читает и пишет в один и тот же экземпляр,
    что эмулирует поведение независимых сессий, видящих коммиты друг друга.
    """

    def __init__(self) -> None:
        self.documents: dict[UUID, Document] = {}
        self.extractions: dict[UUID, Extraction] = {}
        self.review_tasks: dict[UUID, ReviewTask] = {}


class FakeAsyncSession:
    """Минимальная асинхронная сессия для нужд пайплайна и сервисов.

    Поддерживает только то, что вызывает ``ingest.py`` и
    ``_SessionExtractionRepository``: ``scalar`` (с разбором where-условий),
    ``get``, ``add``, ``flush``, ``commit``, ``refresh``.
    """

    def __init__(self, db: FakeDatabase) -> None:
        self._db = db
        self._pending: list[Any] = []

    async def __aenter__(self) -> FakeAsyncSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Незакоммиченные изменения отбрасываются, как в реальной сессии.
        self._pending.clear()

    def _where_filter(self, stmt: Any) -> Any:
        """Извлечь условие ``Column == value`` из простого where-выражения."""
        clause = stmt.whereclause
        if clause is None:
            return None
        # SQLAlchemy: BinaryExpression("==").left — колонка, .right — BindParameter
        column = getattr(clause, "left", None)
        right = getattr(clause, "right", None)
        if column is None or right is None:
            return None
        return getattr(column, "key", None), getattr(right, "value", None)

    async def scalar(self, stmt: Any) -> Any:
        target_entity = None
        descriptions = getattr(stmt, "column_descriptions", [])
        if descriptions:
            target_entity = descriptions[0].get("entity")

        # select(Document).where(Document.object_key == X)
        if target_entity is Document:
            condition = self._where_filter(stmt)
            for document in self._db.documents.values():
                if condition is None:
                    return document
                key, value = condition
                if getattr(document, key, None) == value:
                    return document
            return None

        # select(func.max(Extraction.attempt_no)).where(document_id == X)
        if target_entity is Extraction:
            condition = self._where_filter(stmt)
            document_id = condition[1] if condition else None
            attempt_numbers = [
                extraction.attempt_no
                for extraction in self._db.extractions.values()
                if document_id is None or extraction.document_id == document_id
            ]
            return max(attempt_numbers) if attempt_numbers else None

        return None

    async def get(self, model: type, identity: UUID) -> Any:
        store = {
            Document: self._db.documents,
            Extraction: self._db.extractions,
            ReviewTask: self._db.review_tasks,
        }.get(model)
        if store is None:
            return None
        return store.get(identity)

    def add(self, instance: Any) -> None:
        self._pending.append(instance)

    async def flush(self) -> None:
        # flush переносит объекты в хранилище сразу (id присваивается SQLAlchemy).
        self._apply_pending()

    async def commit(self) -> None:
        self._apply_pending()

    @staticmethod
    def _ensure_id(instance: Any) -> None:
        """Гарантировать наличие id (SQLAlchemy ставит его при реальном flush)."""
        if getattr(instance, "id", None) is None:
            from uuid import uuid4

            instance.id = uuid4()

    def _apply_pending(self) -> None:
        for instance in self._pending:
            store = {
                Document: self._db.documents,
                Extraction: self._db.extractions,
                ReviewTask: self._db.review_tasks,
            }.get(type(instance))
            if store is not None:
                self._ensure_id(instance)
                store[instance.id] = instance
        self._pending.clear()

    async def refresh(self, instance: Any) -> None:
        # Ничего не делаем: объект уже актуален в памяти.
        return None


class FakeSessionFactory:
    """Фабрика сессий, разделяющих одну in-memory БД."""

    def __init__(self, db: FakeDatabase | None = None) -> None:
        self.db = db or FakeDatabase()

    def __call__(self) -> FakeAsyncSession:
        return FakeAsyncSession(self.db)


# ---------------------------------------------------------------------------
# Фейковый сервис извлечения текста
# ---------------------------------------------------------------------------
class FakeTextExtractionService:
    """Возвращает заранее заготовленный результат без обращения к диску."""

    def __init__(self, result: TextExtractionResult | None = None) -> None:
        self._result = result or _make_text_result()
        self.calls: list[str] = []

    def extract(self, source_path: str) -> TextExtractionResult:
        self.calls.append(source_path)
        return self._result


class FailingTextExtractionService:
    """Симулирует отказ на этапе извлечения текста."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def extract(self, source_path: str) -> TextExtractionResult:
        raise self._error


# ---------------------------------------------------------------------------
# Фейковый сервис LLM-экстракции
# ---------------------------------------------------------------------------
class FakeLLMExtractionService:
    """Возвращает готовый документ или поднимает ошибку провайдера.

    Не создаёт ``Extraction`` в БД — имитирует cache-hit или мок: пайплайн
    в этом случае сам заводит экстракцию (``captured is None``).
    """

    def __init__(
        self,
        document: ExtractedDocument | None = None,
        error: Exception | None = None,
    ) -> None:
        self._document = document
        self._error = error
        self.calls: list[Any] = []

    async def extract(
        self, document_id: UUID, text_result: TextExtractionResult
    ) -> ExtractedDocument:
        self.calls.append((document_id, text_result))
        if self._error is not None:
            raise self._error
        assert self._document is not None
        return self._document


# ---------------------------------------------------------------------------
# Фикстуры и хелперы для валидного документа
# ---------------------------------------------------------------------------
def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "подтверждение",
    }


def _field(value: Any, confidence: float = 0.99) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": confidence, "sources": [_source()]}


def _valid_payload() -> dict[str, Any]:
    return {
        "document_type": _field("payment_invoice"),
        "number": _field("42"),
        "date": _field(date(2026, 1, 10).isoformat()),
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


def _make_extracted_document(payload: dict[str, Any] | None = None) -> ExtractedDocument:
    return ExtractedDocument.model_validate(payload or _valid_payload())


def _make_text_result() -> TextExtractionResult:
    return TextExtractionResult(
        source_path="/tmp/fake.pdf",
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


@pytest.fixture()
def storage(tmp_path: Path) -> DocumentStorage:
    return DocumentStorage(root=tmp_path / "storage", max_bytes=1024 * 1024)


@pytest.fixture()
def session_factory() -> FakeSessionFactory:
    return FakeSessionFactory()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Изолированные настройки; БД не используется — только значения полей."""
    return Settings(
        storage_path=tmp_path / "storage",
        max_upload_bytes=1024 * 1024,
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture()
def text_service() -> FakeTextExtractionService:
    return FakeTextExtractionService()


@pytest.fixture()
def llm_service() -> FakeLLMExtractionService:
    return FakeLLMExtractionService(document=_make_extracted_document())


# ===========================================================================
# Успешный путь
# ===========================================================================
async def test_successful_path_creates_document_and_extraction(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    result = await ingest_document(
        file_name="invoice.pdf",
        payload=b"%PDF-1.7 test content",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=False,
    )

    assert "id" in result
    assert result["status"] == DocumentStatus.COMPLETED.value

    document = next(iter(session_factory.db.documents.values()))
    assert document.original_filename == "invoice.pdf"
    assert document.status is DocumentStatus.COMPLETED
    assert document.content_type == "application/pdf"
    assert document.size_bytes == len(b"%PDF-1.7 test content")
    assert document.detected_type is not None
    assert isinstance(document.detected_type, DocumentType)

    # Extraction создана и финализирована.
    extractions = list(session_factory.db.extractions.values())
    assert len(extractions) == 1
    extraction = extractions[0]
    assert extraction.status is ExtractionStatus.SUCCEEDED
    assert extraction.result is not None
    assert extraction.requires_review is False

    # Тайминги и флаг кеша записаны в provider_settings.
    step_durations = extraction.provider_settings.get("step_durations", {})
    assert "text_extraction" in step_durations
    assert "llm_extraction" in step_durations
    assert extraction.provider_settings.get("cache_hit") is False

    # ReviewTask не создаются — guardrails чистые.
    assert len(session_factory.db.review_tasks) == 0


async def test_background_mode_returns_uploaded_immediately(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    """В фоне документ остаётся в uploaded сразу после ответа."""
    result = await ingest_document(
        file_name="invoice.pdf",
        payload=b"%PDF-1.7 test content",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=True,
    )
    assert result["status"] == DocumentStatus.UPLOADED.value


# ===========================================================================
# Повторная загрузка того же файла
# ===========================================================================
async def test_duplicate_upload_returns_existing_document(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    payload = b"%PDF-1.7 duplicate content"
    first = await ingest_document(
        file_name="a.pdf",
        payload=payload,
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=False,
    )

    # Дать фоновой задаче первой загрузки завершиться (она уже отработала —
    # background=False, но для чистоты убедимся, что статус не uploaded).
    first_doc = next(iter(session_factory.db.documents.values()))
    first_doc.status = DocumentStatus.COMPLETED

    second = await ingest_document(
        file_name="b.pdf",  # другое имя, тот же контент
        payload=payload,
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=False,
    )

    assert second["id"] == first["id"]
    assert second.get("already_existed") is True
    # Второго документа не создалось.
    assert len(session_factory.db.documents) == 1


# ===========================================================================
# Превышение лимита размера
# ===========================================================================
async def test_oversized_upload_raises(
    tmp_path: Path,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    small_limit_storage = DocumentStorage(root=tmp_path / "storage", max_bytes=10)
    with pytest.raises(UploadTooLargeError, match="превышает"):
        await ingest_document(
            file_name="big.pdf",
            payload=b"x" * 100,
            session_factory=session_factory,
            settings=settings,
            storage=small_limit_storage,
            text_extraction_service=text_service,
            extraction_service=llm_service,
            background=False,
        )
    # Документ не должен был создаться.
    assert len(session_factory.db.documents) == 0


# ===========================================================================
# Неподдерживаемый тип файла
# ===========================================================================
async def test_unsupported_content_type_raises(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    with pytest.raises(UnsupportedContentTypeError, match="не поддерживается"):
        await ingest_document(
            file_name="readme.txt",
            payload=b"plain text",
            session_factory=session_factory,
            settings=settings,
            storage=storage,
            text_extraction_service=text_service,
            extraction_service=llm_service,
            background=False,
        )
    assert len(session_factory.db.documents) == 0


# ===========================================================================
# Отказ LLM-провайдера
# ===========================================================================
async def test_llm_provider_failure_marks_document_failed(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
) -> None:
    failing_llm = FakeLLMExtractionService(error=LLMProviderError("Ollama is down"))

    result = await ingest_document(
        file_name="broken.pdf",
        payload=b"%PDF-1.7 broken",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=failing_llm,
        background=False,
    )

    assert result["status"] == DocumentStatus.FAILED.value

    document = next(iter(session_factory.db.documents.values()))
    assert document.status is DocumentStatus.FAILED

    # Extraction должна быть создана пайплайном с записью ошибки.
    extractions = list(session_factory.db.extractions.values())
    assert len(extractions) == 1
    extraction = extractions[0]
    assert extraction.status is ExtractionStatus.FAILED
    assert extraction.error_code == "provider_error"
    assert "Ollama is down" in (extraction.error_message or "")


# ===========================================================================
# Создание ReviewTask при сработавшем guardrail
# ===========================================================================
async def test_guardrail_violation_creates_review_task(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
) -> None:
    # Документ с низкой уверенностью вызовет LOW_CONFIDENCE violation.
    payload = _valid_payload()
    payload["number"] = _field("42", confidence=0.5)  # ниже порога 0.80
    low_confidence_doc = _make_extracted_document(payload)

    llm_service = FakeLLMExtractionService(document=low_confidence_doc)

    result = await ingest_document(
        file_name="low_confidence.pdf",
        payload=b"%PDF-1.7 low confidence",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=False,
    )

    assert result["status"] == DocumentStatus.REVIEW_REQUIRED.value

    document = next(iter(session_factory.db.documents.values()))
    assert document.status is DocumentStatus.REVIEW_REQUIRED

    extractions = list(session_factory.db.extractions.values())
    assert len(extractions) == 1
    assert extractions[0].requires_review is True

    # Создан как минимум один ReviewTask.
    review_tasks = list(session_factory.db.review_tasks.values())
    assert len(review_tasks) >= 1
    task = review_tasks[0]
    assert task.status is ReviewTaskStatus.PENDING
    assert task.field_path  # непустой путь
    assert task.reason  # непустая причина


# ===========================================================================
# Отказ на этапе извлечения текста
# ===========================================================================
async def test_text_extraction_failure_marks_document_failed(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    llm_service: FakeLLMExtractionService,
) -> None:
    failing_text = FailingTextExtractionService(RuntimeError("OCR crashed"))

    result = await ingest_document(
        file_name="corrupt.pdf",
        payload=b"%PDF-1.7 corrupt",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=failing_text,
        extraction_service=llm_service,
        background=False,
    )

    assert result["status"] == DocumentStatus.FAILED.value
    document = next(iter(session_factory.db.documents.values()))
    assert document.status is DocumentStatus.FAILED

    extractions = list(session_factory.db.extractions.values())
    assert len(extractions) == 1
    extraction = extractions[0]
    assert extraction.error_code == "text_extraction_failed"
    assert "OCR crashed" in (extraction.error_message or "")


# ===========================================================================
# Дополнительные проверки целостности
# ===========================================================================
async def test_attempt_no_increments_across_uploads(
    storage: DocumentStorage,
    session_factory: FakeSessionFactory,
    settings: Settings,
    text_service: FakeTextExtractionService,
    llm_service: FakeLLMExtractionService,
) -> None:
    """При повторной обработке того же документа attempt_no растёт.

    Симулируем ситуацию, когда документ обрабатывается заново (например,
    после явного «перепрогона» в интерфейсе): новая экстракция того же
    документа должна получить attempt_no=2, а не перезаписать первую.
    """
    # Первая загрузка.
    await ingest_document(
        file_name="doc.pdf",
        payload=b"%PDF-1.7 first",
        session_factory=session_factory,
        settings=settings,
        storage=storage,
        text_extraction_service=text_service,
        extraction_service=llm_service,
        background=False,
    )

    document = next(iter(session_factory.db.documents.values()))
    first_extraction = next(iter(session_factory.db.extractions.values()))
    assert first_extraction.attempt_no == 1

    # Вторая обработка того же документа напрямую (дедуп object_key
    # обходим — он относится только к точке входа ingest_document).
    from docsift.pipeline.ingest import _process_document
    from docsift.pipeline.storage import StoredFile

    stored = storage.save(file_name="doc.pdf", payload=b"%PDF-1.7 first")
    await _process_document(
        document_id=document.id,
        stored=StoredFile(
            object_key=stored.object_key,
            absolute_path=stored.absolute_path,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            content_type=stored.content_type,
            already_existed=True,
        ),
        session_factory=session_factory,
        settings=settings,
        text_extraction_service=FakeTextExtractionService(),
        extraction_service=FakeLLMExtractionService(
            document=_make_extracted_document()
        ),
    )

    extractions = list(session_factory.db.extractions.values())
    # По document_id должно быть две разные экстракции.
    by_document = [
        extraction for extraction in extractions if extraction.document_id == document.id
    ]
    assert len(by_document) == 2
    attempt_numbers = sorted(extraction.attempt_no for extraction in by_document)
    assert attempt_numbers == [1, 2]
