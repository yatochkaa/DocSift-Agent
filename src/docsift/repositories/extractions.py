from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from docsift.db.models import Extraction


class ExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_attempt_no(self, document_id: UUID) -> int:
        latest = await self._session.scalar(
            select(func.max(Extraction.attempt_no)).where(
                Extraction.document_id == document_id
            )
        )
        return (latest or 0) + 1

    async def create(self, extraction: Extraction) -> Extraction:
        self._session.add(extraction)
        await self._commit(extraction)
        return extraction

    async def update(self, extraction: Extraction) -> Extraction:
        await self._commit(extraction)
        return extraction

    async def _commit(self, extraction: Extraction) -> None:
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(extraction)
