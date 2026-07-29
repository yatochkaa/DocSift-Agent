from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from docsift.db.models import Document, Extraction
from docsift.domain.enums import ExtractionStatus


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, document: Document) -> Document:
        self._session.add(document)
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(document)
        return document

    async def get_for_tenant(
        self,
        document_id: UUID,
        tenant_id: UUID,
    ) -> tuple[Document, dict[str, Any] | None] | None:
        document = await self._session.scalar(
            select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id)
        )
        if document is None:
            return None
        result = await self._session.scalar(
            select(Extraction.result)
            .where(
                Extraction.document_id == document_id,
                Extraction.status == ExtractionStatus.SUCCEEDED,
            )
            .order_by(Extraction.attempt_no.desc())
            .limit(1)
        )
        return document, result

