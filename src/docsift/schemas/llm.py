from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]
    json_schema: dict[str, Any]
    schema_name: str = "extracted_document"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
