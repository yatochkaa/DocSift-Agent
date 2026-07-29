from __future__ import annotations

from typing import Any

from pydantic import Field

from docsift.domain.enums import GuardrailRuleCode
from docsift.schemas.common import SchemaModel


class GuardrailViolation(SchemaModel):
    rule: GuardrailRuleCode
    field_path: str = Field(min_length=1)
    message: str = Field(min_length=1)
    expected: Any | None = None
    actual: Any | None = None
    blocking: bool = Field(default=True, description="If True, blocks completion. If False, can be confirmed by user.")


class GuardrailResult(SchemaModel):
    requires_review: bool
    has_warnings: bool = Field(default=False, description="True if there are non-blocking violations only")
    violations: list[GuardrailViolation] = Field(default_factory=list)
