from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from docsift.domain.enums import DocumentStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentCreated(ApiModel):
    id: UUID
    status: DocumentStatus


class DocumentRead(DocumentCreated):
    result: dict[str, Any] | None = None


class HealthResponse(ApiModel):
    status: str

