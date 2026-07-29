from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from docsift.db.models.base import Base, TimestampMixin, UuidPrimaryKeyMixin
from docsift.domain.enums import EvalRunStatus


class EvalRun(UuidPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("sample_count >= 0", name="ck_eval_runs_sample_count_nonnegative"),
    )

    status: Mapped[EvalRunStatus] = mapped_column(
        Enum(EvalRunStatus, name="eval_run_status", native_enum=False, length=32),
        nullable=False,
        default=EvalRunStatus.RUNNING,
    )
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    git_sha: Mapped[str | None] = mapped_column(String(40))
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(String(2000))

