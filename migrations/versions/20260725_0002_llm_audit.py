"""Add LLM extraction audit fields.

Revision ID: 20260725_0002
Revises: 20260725_0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260725_0002"
down_revision = "20260725_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extractions",
        sa.Column("prompt_text", sa.Text(), server_default="", nullable=False),
    )
    op.alter_column("extractions", "prompt_text", server_default=None)
    op.add_column("extractions", sa.Column("raw_response", sa.Text(), nullable=True))
    op.add_column(
        "extractions",
        sa.Column(
            "llm_attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("extractions", "llm_attempts", server_default=None)
    op.add_column("extractions", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("extractions", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("extractions", sa.Column("response_time_ms", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_extractions_input_tokens_nonnegative",
        "extractions",
        "input_tokens IS NULL OR input_tokens >= 0",
    )
    op.create_check_constraint(
        "ck_extractions_output_tokens_nonnegative",
        "extractions",
        "output_tokens IS NULL OR output_tokens >= 0",
    )
    op.create_check_constraint(
        "ck_extractions_response_time_nonnegative",
        "extractions",
        "response_time_ms IS NULL OR response_time_ms >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_extractions_response_time_nonnegative",
        "extractions",
        type_="check",
    )
    op.drop_constraint(
        "ck_extractions_output_tokens_nonnegative",
        "extractions",
        type_="check",
    )
    op.drop_constraint(
        "ck_extractions_input_tokens_nonnegative",
        "extractions",
        type_="check",
    )
    op.drop_column("extractions", "response_time_ms")
    op.drop_column("extractions", "output_tokens")
    op.drop_column("extractions", "input_tokens")
    op.drop_column("extractions", "llm_attempts")
    op.drop_column("extractions", "raw_response")
    op.drop_column("extractions", "prompt_text")
