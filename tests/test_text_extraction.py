from __future__ import annotations

from io import BytesIO
from pathlib import Path

import fitz
from openpyxl import Workbook
from PIL import Image, ImageDraw

from docsift.schemas.text_extraction import BoundingBox, ExtractedTable, TextBlock
from docsift.services.text_extraction import TextExtractionService


class StubOcrEngine:
    def extract(self, image: Image.Image) -> tuple[list[TextBlock], list[ExtractedTable]]:
        return (
            [
                TextBlock(
                    text="Распознанный текст документа",
                    bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                    confidence=0.95,
                    source="ocr:stub",
                )
            ],
            [],
        )


def _make_photo(path: Path, *, rotate: bool = False) -> Path:
    image = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 100), "INVOICE 12345", fill="black")
    draw.text((80, 180), "TOTAL 1000 VAT 200", fill="black")
    if rotate:
        image = image.rotate(2, expand=True, fillcolor="white")
    image.save(path)
    return path


def test_text_pdf_uses_embedded_text_layer(tmp_path: Path) -> None:
    path = tmp_path / "text-document.pdf"
    document = fitz.open()
    page = document.new_page()
    text = (
        "Invoice number 123 Supplier Alpha Buyer Beta Total amount 1000 VAT 200 "
        "Date 2026 07 24 payment goods services contract delivery address"
    )
    page.insert_textbox(fitz.Rect(50, 50, 550, 300), text, fontsize=12)
    document.save(path)
    document.close()

    result = TextExtractionService(ocr_engine=StubOcrEngine()).extract(path)

    assert result.used_ocr is False
    assert result.coordinate_space == "normalized"
    assert result.pages[0].blocks
    assert result.pages[0].blocks[0].source == "pdf:text_layer"


def test_scanned_pdf_renders_page_and_uses_ocr(tmp_path: Path) -> None:
    image_path = _make_photo(tmp_path / "scan-source.png")
    image_bytes = BytesIO()
    with Image.open(image_path) as image:
        image.save(image_bytes, format="PNG")

    path = tmp_path / "scanned-document.pdf"
    document = fitz.open()
    page = document.new_page(width=800, height=500)
    page.insert_image(page.rect, stream=image_bytes.getvalue())
    document.save(path)
    document.close()

    result = TextExtractionService(ocr_engine=StubOcrEngine()).extract(path)

    assert result.used_ocr is True
    assert result.pages[0].used_ocr is True
    assert result.pages[0].blocks
    assert result.pages[0].blocks[0].source == "ocr:stub"


def test_photo_is_preprocessed_and_uses_ocr(tmp_path: Path) -> None:
    path = _make_photo(tmp_path / "chat-photo.jpg", rotate=True)

    result = TextExtractionService(ocr_engine=StubOcrEngine()).extract(path)

    assert result.used_ocr is True
    assert result.media_type == "image/jpeg"
    assert result.pages[0].blocks
    assert result.pages[0].width > 0
    assert result.pages[0].height > 0


def test_csv_is_read_directly_without_ocr(tmp_path: Path) -> None:
    path = tmp_path / "invoice.csv"
    path.write_text("name;quantity;amount\nТовар;2;1000\n", encoding="utf-8")

    result = TextExtractionService(ocr_engine=StubOcrEngine()).extract(path)

    assert result.used_ocr is False
    assert result.pages[0].blocks
    assert result.pages[0].tables[0].rows[1] == ["Товар", "2", "1000"]


def test_xlsx_is_read_directly_without_ocr(tmp_path: Path) -> None:
    path = tmp_path / "invoice.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Счёт"
    worksheet.append(["Наименование", "Количество", "Сумма"])
    worksheet.append(["Услуга", 1, 2500])
    workbook.save(path)
    workbook.close()

    result = TextExtractionService(ocr_engine=StubOcrEngine()).extract(path)

    assert result.used_ocr is False
    assert result.pages[0].label == "Счёт"
    assert result.pages[0].blocks
    assert result.pages[0].tables[0].rows[1] == ["Услуга", "1", "2500"]
