from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from docsift.db.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from docsift.domain.enums import ReviewTaskStatus

if TYPE_CHECKING:
    from docsift.db.models.extraction import Extraction


class ReviewTask(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("ix_review_tasks_status_created_at", "status", "created_at"),
        Index("ix_review_tasks_extraction_field", "extraction_id", "field_path"),
    )

    extraction_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("extractions.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_path: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[ReviewTaskStatus] = mapped_column(
        Enum(ReviewTaskStatus, name="review_task_status", native_enum=False, length=32),
        nullable=False,
        default=ReviewTaskStatus.PENDING,
    )
    original_value: Mapped[Any | None] = mapped_column(JSONB)
    corrected_value: Mapped[Any | None] = mapped_column(JSONB)
    reviewer_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolution_comment: Mapped[str | None] = mapped_column(String(2000))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    extraction: Mapped[Extraction] = relationship("Extraction", back_populates="review_tasks")

