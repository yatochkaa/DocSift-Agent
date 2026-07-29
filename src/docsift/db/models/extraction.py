from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docsift.db.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from docsift.domain.enums import ExtractionStatus

if TYPE_CHECKING:
    from docsift.db.models.document import Document
    from docsift.db.models.review_task import ReviewTask


class Extraction(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "extractions"
    __table_args__ = (
        UniqueConstraint("document_id", "attempt_no", name="uq_extractions_document_attempt"),
        CheckConstraint(
            "overall_confidence IS NULL OR "
            "(overall_confidence >= 0 AND overall_confidence <= 1)",
            name="ck_extractions_confidence_range",
        ),
        Index("ix_extractions_document_status", "document_id", "status"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_extractions_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_extractions_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "response_time_ms IS NULL OR response_time_ms >= 0",
            name="ck_extractions_response_time_nonnegative",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ExtractionStatus] = mapped_column(
        Enum(ExtractionStatus, name="extraction_status", native_enum=False, length=32),
        nullable=False,
        default=ExtractionStatus.PENDING,
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    provider_settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    raw_response: Mapped[str | None] = mapped_column(Text)
    llm_attempts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(2000))

    document: Mapped[Document] = relationship("Document", back_populates="extractions")
    review_tasks: Mapped[list[ReviewTask]] = relationship(
        "ReviewTask",
        back_populates="extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
