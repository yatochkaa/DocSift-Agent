"""Общие фикстуры веб-тестов: приложение с фейковым шлюзом, без БД и без сервера."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from docsift.domain.enums import DocumentStatus
from docsift.web.app import create_standalone_app

NOW = datetime(2025, 3, 12, 14, 5, tzinfo=timezone.utc)


def _document(index: int = 1, status: str = "completed") -> dict[str, Any]:
    return {
        "id": f"doc-{index}",
        "file_name": f"invoice-{index}.pdf",
        "doc_type": "invoice",
        "counterparty": 'ООО «Ромашка»',
        "total_amount": 1234567.5,
        "currency": "₽",
        "doc_date": NOW - timedelta(days=index),
        "status": status,
        "uploaded_at": NOW - timedelta(hours=index),
        "thumbnail_url": None,
        # Размер, тип и код ошибки нужны карточке загрузки — repository отдаёт их
        # вместе с остальными полями документа.
        "size_bytes": 245_760,
        "content_type": "application/pdf",
        "source_url": f"/documents/doc-{index}/source",
        "object_key": f"test/doc-{index}.pdf",
        "error_code": None,
    }


RUN = {
    "run_id": "run-a",
    "started_at": NOW,
    "dataset": "invoices-ru",
    "dataset_version": "3",
    "strategy": "cascade",
    "provider": "ollama",
    "model": "qwen2.5:7b",
    "prompt_version": "v7",
    "documents": 24,
    "accuracy": 0.912,
    "duration_seconds": 184.2,
    "cost": 0.482,
    "step_duration_totals": {"text_extraction": 22.4, "llm_extraction": 141.0, "metrics": 8.6, "other": 12.2},
    "metrics": [
        {"field": "total_amount", "precision": 0.97, "recall": 0.94, "f1": 0.955},
        {"field": "counterparty", "precision": 0.81, "recall": 0.78, "f1": 0.795},
        {"field": "doc_date", "precision": 0.62, "recall": 0.58, "f1": 0.6},
    ],
    "errors": [],
    "samples": [
        {
            "document_id": "doc-1",
            "file_name": "invoice-1.pdf",
            "status": "completed",
            "duration_seconds": 7.4,
            "accuracy": 0.93,
        }
    ],
}

RUN_B = dict(RUN, run_id="run-b", accuracy=0.935, duration_seconds=150.0, cost=0.51, metrics=[
    {"field": "total_amount", "precision": 0.98, "recall": 0.96, "f1": 0.97},
    {"field": "counterparty", "precision": 0.79, "recall": 0.72, "f1": 0.755},
    {"field": "doc_date", "precision": 0.66, "recall": 0.6, "f1": 0.63},
])

EXTRACTED = {
    "doc_type": "invoice",
    "counterparty": 'ООО «Ромашка»',
    "total_amount": 1234567.5,
    "currency": "₽",
    "doc_date": NOW,
    "provider": "ollama",
    "model": "qwen2.5:7b",
    "prompt_version": "v7",
    "total_tokens": 4821,
    "cost": 0.0042,
    "cache_hit": True,
    "fields": [
        {"name": "Сумма", "value": 1234567.5, "confidence": 0.96, "sources": [{"page": 1, "bbox": [0.1, 0.2, 0.4, 0.24]}]},
        {"name": "Контрагент", "value": 'ООО «Ромашка»', "confidence": 0.82, "sources": []},
        {"name": "Дата", "value": "2025-03-12", "confidence": 0.55, "sources": []},
    ],
    "line_items": [
        {"name": "Услуга A", "qty": 2, "amount": 1000.0},
        {"name": "Услуга B", "qty": 1, "amount": 500.0},
    ],
}


class UploadRejected(Exception):
    """Заглушка отказа хранилища. Роут опознаёт такие ошибки по имени класса."""


class UploadTooLargeError(UploadRejected):
    pass


class UnsupportedContentTypeError(UploadRejected):
    pass


class FakeGateway:
    """Фейковый источник данных вместо БД.

    Поведение загрузки и статус документа настраиваются полями: тестам нужно
    проверить и обработку, и готовность к проверке, и отказ, и дубликат.
    """

    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.upload_result: dict[str, Any] = {"id": "doc-new", "status": "processing"}
        self.upload_error: Exception | None = None
        self.document_status: str = "completed"
        self.document_error_code: str | None = None
        self.saved_corrections: list[tuple[str, str, str]] = []
        self.review_completed = False
        self.extraction_result: dict[str, Any] | None = None
        self.review_tasks: list[dict[str, Any]] = []
        self.completion_timestamp: datetime | None = None
        self.deleted_document_ids: set[str] = set()

    async def dashboard(self) -> dict[str, Any]:
        if self.empty:
            return {"documents_current": 0, "documents_previous": 0, "documents_trend": [], "runs": [], "step_duration_totals": {}, "events": []}
        return {
            "documents_current": 128,
            "documents_previous": 96,
            "documents_trend": [4, 9, 6, 12, 15, 11, 18],
            "runs": [dict(RUN, accuracy=0.88), RUN],
            "step_duration_totals": RUN["step_duration_totals"],
            "events": [
                {"kind": "document", "title": "invoice-1.pdf", "subtitle": "invoice", "created_at": NOW, "href": "/documents/doc-1", "chip": "Готово", "tone": "success"},
                {"kind": "run", "title": "Прогон run-a", "subtitle": "cascade", "created_at": NOW, "href": "/evals/run-a", "chip": None},
            ],
        }

    async def documents(self, **kwargs: Any) -> dict[str, Any]:
        if self.empty:
            return {"items": [], "total": 0, "types": [], "statuses": []}
        return {
            "items": [_document(1), _document(2, "processing")],
            "total": 2,
            "types": ["invoice", "act"],
            "statuses": ["completed", "processing"],
        }

    async def document(self, document_id: str) -> dict[str, Any] | None:
        if self.empty or document_id == "missing" or document_id in self.deleted_document_ids:
            return None
        document = _document(1, self.document_status)
        document["id"] = document_id
        document["error_code"] = self.document_error_code
        return {
            "document": document,
            "extracted": EXTRACTED,
            "guardrails": [
                {"rule": "total_matches_line_items", "passed": False, "message": "Сумма позиций не совпадает с итогом"},
                {"rule": "date_in_past", "passed": True, "message": ""},
            ],
            "step_durations": {"text_extraction": 1.2, "llm_extraction": 5.8, "metrics": 0.4},
            "pages": [{"number": 1, "image_url": None, "width": 850, "height": 1100}],
            "review": {
                "open_count": 1 if self.document_status == "review_required" else 0,
                "correction_count": len(self.saved_corrections),
                "can_complete": True,
                "can_export": self.document_status == "completed" or self.review_completed,
            },
        }

    async def runs(self, *, page: int, per_page: int) -> dict[str, Any]:
        if self.empty:
            return {"runs": [], "total": 0}
        return {"runs": [RUN, RUN_B], "total": 2}

    async def run(self, run_id: str) -> dict[str, Any] | None:
        if self.empty or run_id == "missing":
            return None
        return RUN if run_id == "run-a" else RUN_B

    async def run_pair(self, a: str, b: str):
        return await self.run(a), await self.run(b)

    async def upload(self, file_name: str, payload: bytes) -> dict[str, Any]:
        if self.upload_error is not None:
            raise self.upload_error
        return dict(self.upload_result)

    async def save_correction(self, document_id: str, field_path: str, value: str) -> None:
        self.saved_corrections.append((document_id, field_path, value))

    async def save_bulk_corrections(self, document_id: str, corrections: dict[str, str]) -> None:
        for path, value in corrections.items():
            self.saved_corrections.append((document_id, path, value))

    async def delete_document(self, document_id: str) -> None:
        if document_id == "missing":
            raise ValueError("Документ не найден")
        self.deleted_document_ids.add(document_id)

    async def complete_review(
        self,
        document_id: str,
        *,
        confirm_warnings: bool = False,
    ) -> dict[str, Any]:
        """Mirror the production completion contract without a database."""
        if self.review_completed:
            return {
                "completed": True,
                "issues": 0,
                "warnings_confirmed": any(
                    task.get("field_path") == "/review_completion"
                    for task in self.review_tasks
                ),
            }

        if self.extraction_result is None:
            self.review_completed = True
            self.document_status = DocumentStatus.COMPLETED
            self.completion_timestamp = datetime.now(timezone.utc)
            return {"completed": True, "issues": 0, "warnings_confirmed": False}

        from docsift.core.config import get_settings
        from docsift.schemas.documents import ExtractedDocument
        from docsift.services.guardrails import evaluate_guardrails

        try:
            extracted = ExtractedDocument.model_validate(self.extraction_result)
        except Exception as exc:
            # Invalid effective data is a blocking structural error. Never use
            # a validation failure as a backwards-compatible success path.
            return {
                "completed": False,
                "requires_confirmation": False,
                "has_blocking": True,
                "issues": 1,
                "validation_error": str(exc),
            }

        guardrails = evaluate_guardrails(extracted, get_settings())
        blocking = [violation for violation in guardrails.violations if violation.blocking]
        warnings = [violation for violation in guardrails.violations if not violation.blocking]

        findings = [*blocking, *warnings]
        if findings and not confirm_warnings:
            return {
                "completed": False,
                "requires_confirmation": True,
                "has_blocking": bool(blocking),
                "issues": len(findings),
            }

        self.review_completed = True
        self.document_status = DocumentStatus.COMPLETED
        self.completion_timestamp = datetime.now(timezone.utc)
        warnings_confirmed = bool(findings and confirm_warnings)
        if warnings_confirmed:
            self.review_tasks.append(
                {
                    "field_path": "/review_completion",
                    "reason": (
                        "manual_confirmation: "
                        "Завершено с подтверждёнными предупреждениями"
                    ),
                    "status": "resolved",
                    "original_value": "issues_confirmed",
                    "resolution_comment": (
                        "Пользователь подтвердил завершение с "
                        f"{len(findings)} расхождениями"
                    ),
                }
            )
        return {
            "completed": True,
            "issues": len(guardrails.violations),
            "warnings_confirmed": warnings_confirmed,
        }



class _ProcessingEmptyGateway(FakeGateway):
    """Gateway for a document still being processed — no extracted fields."""

    async def document(self, document_id: str) -> dict[str, Any] | None:
        if self.empty or document_id == "missing":
            return None
        doc = _document(1, "processing")
        doc["id"] = document_id
        return {
            "document": doc,
            "extracted": {},
            "guardrails": [],
            "step_durations": {},
            "pages": [],
            "review": {"open_count": 0, "correction_count": 0, "can_complete": False, "can_export": False},
        }


class _FailedEmptyGateway(FakeGateway):
    """Gateway for a document with failed extraction — no extracted fields."""

    async def document(self, document_id: str) -> dict[str, Any] | None:
        if self.empty or document_id == "missing":
            return None
        doc = _document(1, "failed")
        doc["id"] = document_id
        doc["error_code"] = "provider_error"
        return {
            "document": doc,
            "extracted": {},
            "guardrails": [],
            "step_durations": {},
            "pages": [],
            "review": {"open_count": 0, "correction_count": 0, "can_complete": False, "can_export": False},
        }


def make_app(*, empty: bool = False):
    app = create_standalone_app()
    app.state.gateway = FakeGateway(empty=empty)
    return app


@pytest_asyncio.fixture
async def client():
    app = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def upload_client():
    """Клиент вместе со шлюзом: сценарии загрузки настраивают его поведение."""
    app = create_standalone_app()
    gateway = FakeGateway()
    app.state.gateway = gateway
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, gateway


@pytest_asyncio.fixture
async def empty_client():
    app = make_app(empty=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def processing_client():
    app = make_app()
    app.state.gateway = _ProcessingEmptyGateway()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def failed_client():
    app = make_app()
    app.state.gateway = _FailedEmptyGateway()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def now() -> datetime:
    return NOW
