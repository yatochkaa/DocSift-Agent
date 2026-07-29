from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, Enum, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docsift.db.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from docsift.domain.enums import DocumentStatus, DocumentType

if TYPE_CHECKING:
    from docsift.db.models.extraction import Extraction


class Document(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_documents_size_positive"),
        Index("ix_documents_sha256", "sha256"),
        Index("ix_documents_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_documents_status_created_at", "status", "created_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status", native_enum=False, length=32),
        nullable=False,
        default=DocumentStatus.UPLOADED,
    )
    detected_type: Mapped[DocumentType | None] = mapped_column(
        Enum(DocumentType, name="document_type", native_enum=False, length=64),
        nullable=True,
    )

    extractions: Mapped[list[Extraction]] = relationship(
        "Extraction",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
