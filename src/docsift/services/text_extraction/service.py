from __future__ import annotations

from pathlib import Path

from docsift.schemas.text_extraction import TextExtractionResult
from docsift.services.text_extraction.extractors import (
    ImageTextExtractor,
    PdfTextExtractor,
    SpreadsheetTextExtractor,
)
from docsift.services.text_extraction.ocr import OcrEngineProtocol, TesseractOcrEngine
from docsift.services.text_extraction.preprocessing import ImagePreprocessor

_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_SPREADSHEET_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class UnsupportedSourceFileError(ValueError):
    pass


class TextExtractionService:
    def __init__(
        self,
        ocr_engine: OcrEngineProtocol | None = None,
        preprocessor: ImagePreprocessor | None = None,
        pdf_render_dpi: int = 300,
    ) -> None:
        engine = ocr_engine or TesseractOcrEngine()
        image_preprocessor = preprocessor or ImagePreprocessor()
        self._pdf = PdfTextExtractor(engine, image_preprocessor, pdf_render_dpi)
        self._image = ImageTextExtractor(engine, image_preprocessor)
        self._spreadsheet = SpreadsheetTextExtractor()

    def extract(self, source_path: str | Path) -> TextExtractionResult:
        path = Path(source_path)
        if not path.is_file():
            raise FileNotFoundError(path)

        suffix = path.suffix.lower()
        if suffix == ".pdf":
            pages = self._pdf.extract(path)
            media_type = "application/pdf"
        elif suffix in _IMAGE_MEDIA_TYPES:
            pages = self._image.extract(path)
            media_type = _IMAGE_MEDIA_TYPES[suffix]
        elif suffix in _SPREADSHEET_MEDIA_TYPES:
            pages = self._spreadsheet.extract(path)
            media_type = _SPREADSHEET_MEDIA_TYPES[suffix]
        else:
            raise UnsupportedSourceFileError(f"Unsupported source file type: {suffix or '<none>'}")

        return TextExtractionResult(
            source_path=str(path),
            media_type=media_type,
            pages=pages,
            used_ocr=any(page.used_ocr for page in pages),
        )
