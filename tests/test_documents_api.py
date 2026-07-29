from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from docsift.api.dependencies import get_document_service
from docsift.core.config import Settings
from docsift.core.storage import LocalFileStorage
from docsift.db.models import Document
from docsift.main import create_app
from docsift.services.documents import DocumentService


class FakeDocumentRepository:
    def __init__(self) -> None:
        self.documents: dict[UUID, Document] = {}

    async def create(self, document: Document) -> Document:
        self.documents[document.id] = document
        return document

    async def get_for_tenant(
        self,
        document_id: UUID,
        tenant_id: UUID,
    ) -> tuple[Document, dict[str, Any] | None] | None:
        document = self.documents.get(document_id)
        if document is None or document.tenant_id != tenant_id:
            return None
        return document, None


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    settings = Settings(
        storage_path=tmp_path,
        max_upload_bytes=32,
        upload_chunk_bytes=8,
    )
    application = create_app(settings)
    service = DocumentService(
        repository=FakeDocumentRepository(),
        storage=LocalFileStorage(tmp_path),
        settings=settings,
    )
    application.dependency_overrides[get_document_service] = lambda: service
    return application


async def request(app: FastAPI, method: str, url: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_health(app: FastAPI) -> None:
    response = await request(app, "GET", "/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_successful_upload(app: FastAPI, tmp_path: Path) -> None:
    tenant_id = uuid4()
    response = await request(
        app,
        "POST",
        "/documents",
        headers={"X-Tenant-ID": str(tenant_id)},
        files={"file": ("invoice.pdf", b"%PDF-1.7 test", "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "uploaded"
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


@pytest.mark.asyncio
async def test_rejects_unsupported_type(app: FastAPI) -> None:
    response = await request(
        app,
        "POST",
        "/documents",
        headers={"X-Tenant-ID": str(uuid4())},
        files={"file": ("notes.txt", b"text", "text/plain")},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_rejects_file_above_limit(app: FastAPI) -> None:
    response = await request(
        app,
        "POST",
        "/documents",
        headers={"X-Tenant-ID": str(uuid4())},
        files={"file": ("large.pdf", b"%PDF-" + b"x" * 32, "application/pdf")},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_foreign_tenant_receives_404(app: FastAPI) -> None:
    owner_id = uuid4()
    upload = await request(
        app,
        "POST",
        "/documents",
        headers={"X-Tenant-ID": str(owner_id)},
        files={"file": ("invoice.pdf", b"%PDF-1.7 test", "application/pdf")},
    )
    document_id = upload.json()["id"]

    response = await request(
        app,
        "GET",
        f"/documents/{document_id}",
        headers={"X-Tenant-ID": str(uuid4())},
    )
    assert response.status_code == 404

