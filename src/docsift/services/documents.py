from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from fastapi import UploadFile

from docsift.core.config import Settings
from docsift.core.storage import LocalFileStorage
from docsift.db.models import Document
from docsift.domain.enums import DocumentStatus
from docsift.domain.exceptions import DocumentNotFoundError, UnsupportedFileTypeError


class DocumentRepositoryProtocol(Protocol):
    async def create(self, document: Document) -> Document: ...

    async def get_for_tenant(
        self,
        document_id: UUID,
        tenant_id: UUID,
    ) -> tuple[Document, dict[str, Any] | None] | None: ...


@dataclass(frozen=True, slots=True)
class DocumentView:
    id: UUID
    status: DocumentStatus
    result: dict[str, Any] | None


_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}


def _matches_signature(content_type: str, header: bytes) -> bool:
    if content_type == "application/pdf":
        return header.startswith(b"%PDF-")
    if content_type == "application/vnd.ms-excel":
        return header.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    if content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return header.startswith(b"PK\x03\x04")
    if content_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/tiff":
        return header.startswith((b"II*\x00", b"MM\x00*"))
    if content_type == "image/webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return False


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepositoryProtocol,
        storage: LocalFileStorage,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._settings = settings

    async def upload(self, tenant_id: UUID, upload: UploadFile) -> DocumentView:
        content_type = upload.content_type or ""
        if content_type not in self._settings.allowed_content_types:
            raise UnsupportedFileTypeError

        header = await upload.read(16)
        await upload.seek(0)
        if not _matches_signature(content_type, header):
            raise UnsupportedFileTypeError

        document_id = uuid4()
        object_key = f"{tenant_id}/{document_id}{_SUFFIXES[content_type]}"
        stored = await self._storage.put(
            upload,
            object_key,
            self._settings.max_upload_bytes,
            self._settings.upload_chunk_bytes,
        )
        filename = Path((upload.filename or "document").replace("\\", "/")).name[:512]
        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            original_filename=filename,
            object_key=stored.object_key,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            status=DocumentStatus.UPLOADED,
        )
        try:
            await self._repository.create(document)
        except Exception:
            await self._storage.delete(stored.object_key)
            raise
        return DocumentView(id=document.id, status=document.status, result=None)

    async def get(self, tenant_id: UUID, document_id: UUID) -> DocumentView:
        found = await self._repository.get_for_tenant(document_id, tenant_id)
        if found is None:
            raise DocumentNotFoundError
        document, result = found
        return DocumentView(id=document.id, status=document.status, result=result)

