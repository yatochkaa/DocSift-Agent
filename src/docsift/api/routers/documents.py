from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status

from docsift.api.dependencies import get_document_service
from docsift.domain.exceptions import (
    DocumentNotFoundError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from docsift.schemas.api import DocumentCreated, DocumentRead
from docsift.services.documents import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])
TenantId = Annotated[UUID, Header(alias="X-Tenant-ID")]
UploadedFile = Annotated[UploadFile, File()]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


@router.post("", response_model=DocumentCreated, status_code=status.HTTP_201_CREATED)
async def upload_document(
    tenant_id: TenantId,
    file: UploadedFile,
    service: DocumentServiceDep,
) -> DocumentCreated:
    try:
        document = await service.upload(tenant_id, file)
    except UnsupportedFileTypeError as error:
        raise HTTPException(status_code=415, detail="Unsupported file type") from error
    except FileTooLargeError as error:
        raise HTTPException(status_code=413, detail="File is too large") from error
    finally:
        await file.close()
    return DocumentCreated(id=document.id, status=document.status)


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: UUID,
    tenant_id: TenantId,
    service: DocumentServiceDep,
) -> DocumentRead:
    try:
        document = await service.get(tenant_id, document_id)
    except DocumentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Document not found") from error
    return DocumentRead(id=document.id, status=document.status, result=document.result)
