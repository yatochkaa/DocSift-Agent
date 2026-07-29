from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from docsift.core.config import Settings
from docsift.core.storage import LocalFileStorage
from docsift.repositories.documents import DocumentRepository
from docsift.services.documents import DocumentService


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_document_service(
    session: SessionDep,
    settings: SettingsDep,
) -> DocumentService:
    return DocumentService(
        repository=DocumentRepository(session),
        storage=LocalFileStorage(settings.storage_path),
        settings=settings,
    )
