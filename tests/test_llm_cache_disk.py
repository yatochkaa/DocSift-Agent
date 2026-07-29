r"""Тесты дискового кеша результатов извлечения.

Куда класть: tests/test_llm_cache_disk.py

Запуск::

    .\.venv\Scripts\python.exe -m pytest tests/test_llm_cache_disk.py -v --basetemp=.pytest-tmp
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.text_extraction import (
    BoundingBox,
    ExtractedPage,
    TextBlock,
    TextExtractionResult,
)
from docsift.services.llm.cache import (
    CacheKey,
    DiskExtractionCache,
    ExtractionCache,
    content_hash,
)

MODEL = "qwen2.5-coder:7b"


def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "Счёт № 143",
    }


def _field(value: Any) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": 0.99, "sources": [_source()]}


def _document(number: str = "143") -> ExtractedDocument:
    return ExtractedDocument.model_validate(
        {
            "document_type": _field("payment_invoice"),
            "number": _field(number),
            "date": _field("2025-03-12"),
            "supplier": {
                "name": _field("ООО Ромашка"),
                "inn": _field("7714236789"),
                "kpp": _field("771401001"),
            },
            "buyer": {
                "name": _field("ООО ТехноСервис Плюс"),
                "inn": _field("5029154872"),
                "kpp": _field("502901001"),
            },
            "total_amount": _field("69564.00"),
            "vat_amount": _field("11594.00"),
            "currency": _field("RUB"),
            "line_items": [],
        }
    )


def _text_result(source_path: str = "datasets/doc_01.pdf", text: str = "Счёт № 143") -> TextExtractionResult:
    return TextExtractionResult(
        source_path=source_path,
        media_type="application/pdf",
        pages=[
            ExtractedPage(
                number=1,
                width=595,
                height=842,
                blocks=[
                    TextBlock(
                        text=text,
                        bbox=BoundingBox(x0=0.1, y0=0.1, x1=0.9, y1=0.2),
                        confidence=1,
                        source="pdf:text_layer",
                    )
                ],
            )
        ],
        used_ocr=False,
    )


# ---------------------------------------------------------------------------
# Ключ
# ---------------------------------------------------------------------------


def test_key_ignores_the_file_path() -> None:
    """Один и тот же документ из разных папок должен делить запись кеша."""
    assert content_hash(_text_result("a/doc.pdf")) == content_hash(_text_result("b/doc.pdf"))


def test_key_changes_with_content() -> None:
    assert content_hash(_text_result(text="Счёт № 143")) != content_hash(
        _text_result(text="Счёт № 144")
    )


def test_file_name_survives_windows(tmp_path: Path) -> None:
    """В имени модели есть двоеточие, а в именах файлов Windows оно запрещено."""
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)

    name = cache.path_for(key).name

    assert ":" not in name
    assert name.endswith(".json")
    cache.store(key, _document())
    assert cache.path_for(key).is_file()


# ---------------------------------------------------------------------------
# Чтение и запись
# ---------------------------------------------------------------------------


def test_miss_on_empty_directory(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)

    assert cache.get(cache.make_key(_text_result(), "v3", MODEL)) is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_stored_document_is_returned_unchanged(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    document = _document()

    cache.store(key, document)
    entry = cache.get(key)

    assert entry is not None
    assert entry.document.model_dump(mode="json") == document.model_dump(mode="json")
    assert entry.model == MODEL
    assert entry.prompt_version == "v3"


def test_cache_survives_a_new_process(tmp_path: Path) -> None:
    """Ради этого всё и затевалось: in-memory кеш здесь вернул бы None."""
    key = DiskExtractionCache(tmp_path).make_key(_text_result(), "v3", MODEL)
    DiskExtractionCache(tmp_path).store(key, _document())

    entry = DiskExtractionCache(tmp_path).get(key)

    assert entry is not None
    assert entry.document.number.value == "143"


def test_in_memory_cache_does_not_survive(tmp_path: Path) -> None:
    """Контраст с дисковым: так было до правки."""
    key = ExtractionCache().make_key(_text_result(), "v3", MODEL)
    ExtractionCache().store(key, _document())

    assert ExtractionCache().get(key) is None


# ---------------------------------------------------------------------------
# Инвалидация
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt_version", "model"),
    [("v1", MODEL), ("v3", "qwen2.5-coder:14b")],
)
def test_other_prompt_or_model_is_a_miss(tmp_path: Path, prompt_version: str, model: str) -> None:
    """Смена промта или модели обязана обесценить старый ответ."""
    cache = DiskExtractionCache(tmp_path)
    cache.store(cache.make_key(_text_result(), "v3", MODEL), _document())

    assert cache.get(cache.make_key(_text_result(), prompt_version, model)) is None


def test_other_document_is_a_miss(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    cache.store(cache.make_key(_text_result(), "v3", MODEL), _document())

    assert cache.get(cache.make_key(_text_result(text="Счёт № 999"), "v3", MODEL)) is None


def test_broken_file_is_treated_as_a_miss_and_removed(tmp_path: Path) -> None:
    """Оборванная запись не должна ронять прогон."""
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())
    cache.path_for(key).write_text("{это не json", encoding="utf-8")

    assert cache.get(key) is None
    assert not cache.path_for(key).exists()


def test_document_failing_validation_is_a_miss(tmp_path: Path) -> None:
    """Старая запись после смены схемы документа не должна подцепиться."""
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())
    path = cache.path_for(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["document"] = {"чушь": True}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert cache.get(key) is None


def test_key_mismatch_inside_the_file_is_a_miss(tmp_path: Path) -> None:
    """Защита от чужого файла, положенного под тем же именем."""
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())
    path = cache.path_for(key)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["key"] = str(CacheKey(content_hash="чужой", prompt_version="v3", model=MODEL))
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert cache.get(key) is None


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------


def test_stats_count_hits_misses_and_entries(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.get(key)
    cache.store(key, _document())
    cache.get(key)
    cache.get(key)

    assert cache.stats() == {
        "entries": 1,
        "hits": 2,
        "misses": 1,
        "directory": str(tmp_path),
    }


def test_hit_counter_is_persisted(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())

    cache.get(key)
    second = DiskExtractionCache(tmp_path).get(key)

    assert second is not None
    assert second.hits == 2


def test_clear_empties_the_directory(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())

    cache.clear()

    assert cache.get(key) is None
    assert cache.stats()["entries"] == 0


def test_directory_is_created_lazily(tmp_path: Path) -> None:
    """Пустой прогон без записей не должен создавать мусорных каталогов."""
    directory = tmp_path / "var" / "llm-cache"
    cache = DiskExtractionCache(directory)

    assert cache.get(cache.make_key(_text_result(), "v3", MODEL)) is None
    assert not directory.exists()

    cache.store(cache.make_key(_text_result(), "v3", MODEL), _document())
    assert directory.is_dir()


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    cache = DiskExtractionCache(tmp_path)
    key = cache.make_key(_text_result(), "v3", MODEL)
    cache.store(key, _document())
    cache.get(key)

    assert list(tmp_path.glob("*.tmp")) == []
