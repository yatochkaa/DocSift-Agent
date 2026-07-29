from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_order(self) -> BoundingBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("Bounding box end must not precede its start")
        return self


class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    bbox: BoundingBox
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(min_length=1)


class ExtractedTable(BaseModel):
    model_config = ConfigDict(frozen=True)

    rows: list[list[str]] = Field(min_length=1)
    bbox: BoundingBox | None = None
    source: str = Field(min_length=1)


class ExtractedPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int = Field(ge=1)
    label: str | None = None
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    blocks: list[TextBlock] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    used_ocr: bool = False


class TextExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    coordinate_space: Literal["normalized"] = "normalized"
    pages: list[ExtractedPage] = Field(min_length=1)
    used_ocr: bool
