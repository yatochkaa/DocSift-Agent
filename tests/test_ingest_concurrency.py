"""Тесты ограничения параллельной обработки документов (семафор)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from docsift.core.config import Settings
from docsift.domain.enums import DocumentStatus
from docsift.pipeline import ingest_document
from docsift.pipeline.storage import DocumentStorage

from tests.test_pipeline_ingest import (
    FakeDatabase,
    FakeSessionFactory,
    FakeTextExtractionService,
    FakeLLMExtractionService,
    _make_extracted_document,
)


# ---------------------------------------------------------------------------
# Текстовый сервис, фиксирующий одновременность
# ---------------------------------------------------------------------------
class ConcurrencyProbe:
    """Счётчик активных вызовов: +1 на входе, −1 на выходе.

    Между входом и выходом делает ``await asyncio.sleep(0)`` несколько раз,
    чтобы дать другим задачам шанс войти внутрь семафора.
    """

    def __init__(self, repeats: int = 5) -> None:
        self.active = 0
        self.max_observed = 0
        self._repeats = repeats

    async def run(self) -> None:
        self.active += 1
        if self.active > self.max_observed:
            self.max_observed = self.active
        try:
            for _ in range(self._repeats):
                await asyncio.sleep(0)
        finally:
            self.active -= 1


class TrackedTextService:
    """Фейковый текстовый сервис с привязкой к ``ConcurrencyProbe``."""

    def __init__(self, probe: ConcurrencyProbe) -> None:
        self._probe = probe
        self.calls: list[str] = []

    def extract(self, source_path: str) -> Any:
        self.calls.append(source_path)
        # extract — синхронная; запускаем busy-wait через loop чтобы дать
        # другим задачам шанс (to_thread в реальном пайплайне и так даёт
        # переключение, но в тестах это синхронно).
        return _make_text_result()


def _make_text_result() -> Any:
    from docsift.schemas.text_extraction import (
        BoundingBox,
        ExtractedPage,
        TextBlock,
        TextExtractionResult,
    )

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
                        text="test",
                        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        confidence=1,
                        source="pdf:text_layer",
                    )
                ],
            )
        ],
        used_ocr=False,
    )


class ConcurrencyTrackingLLM:
    """LLM-сервис: замеряет пиковую одновременность через ``ConcurrencyProbe``."""

    def __init__(self, probe: ConcurrencyProbe) -> None:
        self._probe = probe
        self.calls: list[Any] = []

    async def extract(self, document_id: UUID, text_result: Any) -> Any:
        self.calls.append(document_id)
        # Эмулируем тяжёлую операцию: входим в «активные», ждём, выходим.
        await self._probe.run()
        return _make_extracted_document()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _settings(tmp_path: Path, max_concurrency: int = 1) -> Settings:
    return Settings(
        storage_path=tmp_path / "storage",
        max_upload_bytes=1024 * 1024,
        database_url="sqlite+aiosqlite:///:memory:",
        llm_max_concurrency=max_concurrency,
    )


async def _run_concurrent_ingest(
    *,
    tmp_path: Path,
    num_docs: int,
    max_concurrency: int,
) -> tuple[int, list[str]]:
    """Запустить *num_docs* параллельных загрузок и вернуть (макс. одновременности, статусы).

    Возвращает кортеж ``(max_observed, [status, ...])``.
    """
    settings = _settings(tmp_path, max_concurrency)
    db = FakeDatabase()
    factory = FakeSessionFactory(db)
    storage = DocumentStorage(root=tmp_path / "storage", max_bytes=1024 * 1024)

    probe = ConcurrencyProbe()
    text_svc = TrackedTextService(probe)
    llm_svc = ConcurrencyTrackingLLM(probe)

    tasks = []
    for i in range(num_docs):
        tasks.append(
            ingest_document(
                file_name=f"doc-{i}.pdf",
                payload=f"%PDF-1.7 unique content {i}".encode(),
                session_factory=factory,
                settings=settings,
                storage=storage,
                text_extraction_service=text_svc,
                extraction_service=llm_svc,
                background=False,
            )
        )

    results = await asyncio.gather(*tasks)
    statuses = [r["status"] for r in results]
    return probe.max_observed, statuses


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


async def test_max_concurrency_1(tmp_path: Path) -> None:
    """При llm_max_concurrency=1 и 5 документах максимум одновременности = 1."""
    max_obs, statuses = await _run_concurrent_ingest(
        tmp_path=tmp_path, num_docs=5, max_concurrency=1
    )
    assert max_obs == 1
    # Все документы обработались.
    assert all(s == DocumentStatus.COMPLETED.value for s in statuses)


async def test_max_concurrency_3(tmp_path: Path) -> None:
    """При llm_max_concurrency=3 и 6 документах: 1 < max ≤ 3."""
    max_obs, statuses = await _run_concurrent_ingest(
        tmp_path=tmp_path, num_docs=6, max_concurrency=3
    )
    assert max_obs <= 3, f"Одновременность {max_obs} > лимита 3"
    assert max_obs > 1, (
        f"Одновременность {max_obs} = 1: семафор не влияет на параллельность"
    )
    assert all(s in (DocumentStatus.COMPLETED.value, DocumentStatus.REVIEW_REQUIRED.value)
               for s in statuses)


async def test_all_documents_reach_final_status(tmp_path: Path) -> None:
    """Ни один документ не остаётся в uploaded и не падает из-за семафора."""
    for concurrency in (1, 3):
        _, statuses = await _run_concurrent_ingest(
            tmp_path=tmp_path, num_docs=5, max_concurrency=concurrency
        )
        for s in statuses:
            assert s != DocumentStatus.UPLOADED.value, (
                f"Документ остался в uploaded при concurrency={concurrency}"
            )
            assert s != DocumentStatus.FAILED.value, (
                f"Документ упал в failed из-за семафора при concurrency={concurrency}"
            )


def _sync_scenario(tmp_path: Path) -> tuple[int, list[str]]:
    """Запуск сценария в отдельном event loop через asyncio.run."""

    async def _inner() -> tuple[int, list[str]]:
        return await _run_concurrent_ingest(
            tmp_path=tmp_path, num_docs=3, max_concurrency=2
        )

    return asyncio.run(_inner())


def test_event_loop_stability_first(tmp_path: Path) -> None:
    """Сценарий в первом event loop — без ошибок привязки семафора."""
    max_obs, statuses = _sync_scenario(tmp_path)
    assert max_obs <= 2
    assert all(s != DocumentStatus.FAILED.value for s in statuses)


def test_event_loop_stability_second(tmp_path: Path) -> None:
    """Сценарий во втором event loop — без ошибок привязки семафора."""
    max_obs, statuses = _sync_scenario(tmp_path)
    assert max_obs <= 2
    assert all(s != DocumentStatus.FAILED.value for s in statuses)


async def test_background_false_returns_final_status(tmp_path: Path) -> None:
    """Ветка background=False продолжает работать и возвращает финальный статус."""
    settings = _settings(tmp_path, max_concurrency=2)
    db = FakeDatabase()
    factory = FakeSessionFactory(db)
    storage = DocumentStorage(root=tmp_path / "storage", max_bytes=1024 * 1024)

    result = await ingest_document(
        file_name="sync.pdf",
        payload=b"%PDF-1.7 test content",
        session_factory=factory,
        settings=settings,
        storage=storage,
        text_extraction_service=FakeTextExtractionService(),
        extraction_service=FakeLLMExtractionService(document=_make_extracted_document()),
        background=False,
    )

    assert result["status"] == DocumentStatus.COMPLETED.value
    document = next(iter(db.documents.values()))
    assert document.status is DocumentStatus.COMPLETED
