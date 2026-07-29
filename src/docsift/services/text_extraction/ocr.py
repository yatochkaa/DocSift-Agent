from __future__ import annotations

from typing import Protocol

import pytesseract
from PIL import Image

from docsift.schemas.text_extraction import BoundingBox, ExtractedTable, TextBlock


class OcrEngineProtocol(Protocol):
    def extract(self, image: Image.Image) -> tuple[list[TextBlock], list[ExtractedTable]]: ...


class TesseractOcrEngine:
    def __init__(self, languages: str = "rus+eng") -> None:
        self._languages = languages
        self._config = "--oem 1 --psm 6 -c preserve_interword_spaces=1"

    def extract(self, image: Image.Image) -> tuple[list[TextBlock], list[ExtractedTable]]:
        data = pytesseract.image_to_data(
            image,
            lang=self._languages,
            config=self._config,
            output_type=pytesseract.Output.DICT,
        )
        blocks: list[TextBlock] = []
        width, height = image.size

        for index, raw_text in enumerate(data["text"]):
            text = raw_text.strip()
            if not text:
                continue
            try:
                raw_confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                raw_confidence = 0.0
            if raw_confidence < 0:
                continue

            left = int(data["left"][index])
            top = int(data["top"][index])
            block_width = int(data["width"][index])
            block_height = int(data["height"][index])
            blocks.append(
                TextBlock(
                    text=text,
                    bbox=BoundingBox(
                        x0=max(0.0, min(1.0, left / width)),
                        y0=max(0.0, min(1.0, top / height)),
                        x1=max(0.0, min(1.0, (left + block_width) / width)),
                        y1=max(0.0, min(1.0, (top + block_height) / height)),
                    ),
                    confidence=max(0.0, min(1.0, raw_confidence / 100)),
                    source="ocr:tesseract",
                )
            )

        if not blocks:
            text = pytesseract.image_to_string(
                image,
                lang=self._languages,
                config=self._config,
            ).strip()
            if text:
                blocks.append(
                    TextBlock(
                        text=text,
                        bbox=BoundingBox(x0=0.0, y0=0.0, x1=1.0, y1=1.0),
                        confidence=0.5,
                        source="ocr:tesseract",
                    )
                )

        return blocks, []
