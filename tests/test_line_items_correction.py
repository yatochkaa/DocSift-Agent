"""Tests for line_items correction functionality in review workflow."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

import pytest

from docsift.db.models import Document, Extraction
from docsift.domain.enums import DocumentStatus, ExtractionStatus
from docsift.web.repository import (
    _apply_corrections,
    _correction_map,
    _field_container,
    _json_pointer_parts,
    complete_document_review,
)


@pytest.mark.parametrize("path,expected", [
    ("/line_items/0/vat_amount", ["line_items", 0, "vat_amount"]),
    ("/buyer/inn", ["buyer", "inn"]),
    ("/line_items/2/description", ["line_items", 2, "description"]),
    ("/supplier/kpp", ["supplier", "kpp"]),
])
def test_json_pointer_parts_parsing(path, expected):
    """Test that JSON Pointer paths are correctly parsed."""
    assert _json_pointer_parts(path) == expected


@pytest.mark.parametrize("index,should_fail", [
    (0, False),
    (2, False),
    (-1, True),  # Negative index should fail
    (10, True),  # Out of bounds should fail
])
def test_field_container_validates_array_indices(index, should_fail):
    """Test that _field_container properly validates array indices."""
    data = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0}},
            {"name": {"value": "Item 2"}, "vat_amount": {"value": 200.0}},
            {"name": {"value": "Item 3"}, "vat_amount": {"value": 300.0}},
        ]
    }
    
    path = f"/line_items/{index}/vat_amount"
    
    if should_fail:
        with pytest.raises(ValueError, match="Отрицательный индекс не допускается|выходит за границы списка"):
            _field_container(data, path)
    else:
        field = _field_container(data, path)
        assert field == {"value": data["line_items"][index]["vat_amount"]["value"]}


def test_field_container_rejects_unknown_paths():
    """Test that _field_container rejects paths that don't exist."""
    data = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0}},
        ]
    }
    
    with pytest.raises(ValueError, match="Поле 'nonexistent' не найдено"):
        _field_container(data, "/line_items/0/nonexistent")


def test_field_container_rejects_non_field_paths():
    """Test that _field_container rejects paths that don't point to field containers."""
    data = {
        "line_items": [
            {"name": {"value": "Item 1"}},  # Missing "value" at leaf
        ]
    }
    
    with pytest.raises(ValueError, match="не является контейнером поля"):
        _field_container(data, "/line_items/0")


def test_correction_filters_by_status():
    """Test that _correction_map only includes resolved tasks."""
    class MockTask:
        def __init__(self, field_path, status, corrected_value=None, reason=None):
            self.field_path = field_path
            self.status = status
            self.corrected_value = corrected_value
            self.reason = reason
    
    tasks = [
        MockTask("/line_items/0/vat_amount", "resolved", 250.0, "manual_correction: fixed"),
        MockTask("/buyer/inn", "pending", None, "vat_rate_mismatch: error"),
        MockTask("/supplier/name", "rejected", "ООО Тест", "manual_correction: rejected"),
    ]
    
    corrections = _correction_map(tasks)
    assert corrections == {"/line_items/0/vat_amount": 250.0}


def test_apply_corrections_for_line_items():
    """Test that _apply_corrections correctly applies corrections to line_items."""
    raw = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0}},
            {"name": {"value": "Item 2"}, "vat_amount": {"value": 200.0}},
        ]
    }
    corrections = {"/line_items/0/vat_amount": 250.0}
    
    result = _apply_corrections(raw, corrections)
    
    assert result["line_items"][0]["vat_amount"]["value"] == 250.0
    assert result["line_items"][1]["vat_amount"]["value"] == 200.0  # Unchanged


def test_apply_corrections_does_not_mutate_raw():
    """Test that _apply_corrections does not modify the original raw dict."""
    raw = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0}},
        ]
    }
    original_raw = deepcopy(raw)
    corrections = {"/line_items/0/vat_amount": 250.0}
    
    result = _apply_corrections(raw, corrections)
    
    assert raw == original_raw  # Original unchanged
    assert result["line_items"][0]["vat_amount"]["value"] == 250.0  # Result changed


def test_apply_corrections_handles_invalid_paths_gracefully():
    """Test that _apply_corrections skips invalid paths without failing."""
    raw = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0}},
        ]
    }
    corrections = {
        "/line_items/0/vat_amount": 250.0,  # Valid
        "/line_items/10/invalid": "test",  # Invalid - out of bounds
        "/nonexistent": "test",  # Invalid - doesn't exist
    }
    
    result = _apply_corrections(raw, corrections)
    
    assert result["line_items"][0]["vat_amount"]["value"] == 250.0


def test_apply_corrections_sets_confidence_and_sources():
    """Test that corrections set confidence to 1.0 and add manual sources."""
    raw = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0, "confidence": 0.5}},
        ]
    }
    corrections = {"/line_items/0/vat_amount": 250.0}
    
    result = _apply_corrections(raw, corrections)
    
    field = result["line_items"][0]["vat_amount"]
    assert field["confidence"] == 1.0
    assert field["sources"] == [{"kind": "pdf_text", "page": 1, "text": "Подтверждено при ручной проверке"}]


def test_apply_corrections_handles_null_values():
    """Test that corrections to None set confidence to 0.0 and clear sources."""
    raw = {
        "line_items": [
            {"name": {"value": "Item 1"}, "vat_amount": {"value": 100.0, "sources": [{"kind": "pdf"}]}},
        ]
    }
    corrections = {"/line_items/0/vat_amount": ""}
    
    result = _apply_corrections(raw, corrections)
    
    field = result["line_items"][0]["vat_amount"]
    assert field["value"] == ""
    assert field["confidence"] == 0.0
    assert field["sources"] == []


def test_nested_list_and_dict_navigation():
    """Test complex navigation through nested lists and dicts."""
    data = {
        "line_items": [
            {
                "name": {"value": "Item 1"},
                "details": {
                    "price": {"value": 100.0},
                    "vat": {"value": 20.0}
                }
            }
        ]
    }
    
    field = _field_container(data, "/line_items/0/details/price")
    assert field == {"value": 100.0}


# ---------------------------------------------------------------------------
# Status guard tests for complete_document_review
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return self._items


class _FakeSession:
    def __init__(self, document: Document | None, extraction: Extraction | None) -> None:
        self._document = document
        self._extraction = extraction
        self.committed = False

    async def get(self, model: type, identity: UUID) -> Any:
        if model is Document and self._document is not None and self._document.id == identity:
            return self._document
        return None

    async def execute(self, stmt: Any) -> Any:
        descriptions = getattr(stmt, "column_descriptions", [])
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is Extraction:
            return _FakeResult([self._extraction] if self._extraction else [])
        return _FakeResult([])

    def add(self, instance: Any) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True


def _make_document(status: DocumentStatus) -> Document:
    doc = Document(
        id=uuid4(),
        tenant_id=uuid4(),
        original_filename="test.pdf",
        object_key="test/key.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        status=status,
    )
    return doc


def _make_extraction(document_id: UUID) -> Extraction:
    ext = Extraction(
        id=uuid4(),
        document_id=document_id,
        attempt_no=1,
        status=ExtractionStatus.SUCCEEDED,
        schema_version="1",
        provider="ollama",
        model="qwen2.5:7b",
        prompt_version="v1",
        prompt_text="",
        provider_settings={},
        llm_attempts=[],
    )
    src = [{"kind": "pdf_text", "page": 1, "bbox": {"x1": 0, "y1": 0, "x2": 0.5, "y2": 0.5}}]
    ext.result = {
        "document_type": {"value": "payment_invoice", "confidence": 0.95, "sources": src},
        "number": {"value": "42", "confidence": 0.95, "sources": src},
        "date": {"value": "2025-03-12", "confidence": 0.95, "sources": src},
        "supplier": {
            "name": {"value": "ООО Тест", "confidence": 0.95, "sources": src},
            "inn": {"value": "7707083893", "confidence": 0.95, "sources": src},
            "kpp": {"value": "773601001", "confidence": 0.95, "sources": src},
        },
        "buyer": {
            "name": {"value": "ИП Тест", "confidence": 0.95, "sources": src},
            "inn": {"value": "500100732259", "confidence": 0.95, "sources": src},
            "kpp": {"value": None, "confidence": 0, "sources": []},
        },
        "total_amount": {"value": "1200.00", "confidence": 0.95, "sources": src},
        "vat_amount": {"value": "200.00", "confidence": 0.95, "sources": src},
        "currency": {"value": "RUB", "confidence": 0.95, "sources": src},
        "line_items": [],
    }
    return ext


@pytest.mark.asyncio
async def test_complete_review_rejects_uploaded_status():
    from docsift.core.config import Settings
    doc = _make_document(DocumentStatus.UPLOADED)
    ext = _make_extraction(doc.id)
    session = _FakeSession(doc, ext)
    with pytest.raises(ValueError, match="не находится в статусе"):
        await complete_document_review(session, str(doc.id), Settings())
    assert not session.committed
    assert doc.status is DocumentStatus.UPLOADED


@pytest.mark.asyncio
async def test_complete_review_rejects_processing_status():
    from docsift.core.config import Settings
    doc = _make_document(DocumentStatus.PROCESSING)
    ext = _make_extraction(doc.id)
    session = _FakeSession(doc, ext)
    with pytest.raises(ValueError, match="не находится в статусе"):
        await complete_document_review(session, str(doc.id), Settings())
    assert not session.committed
    assert doc.status is DocumentStatus.PROCESSING


@pytest.mark.asyncio
async def test_complete_review_rejects_failed_status():
    from docsift.core.config import Settings
    doc = _make_document(DocumentStatus.FAILED)
    ext = _make_extraction(doc.id)
    session = _FakeSession(doc, ext)
    with pytest.raises(ValueError, match="не находится в статусе"):
        await complete_document_review(session, str(doc.id), Settings())
    assert not session.committed
    assert doc.status is DocumentStatus.FAILED


@pytest.mark.asyncio
async def test_complete_review_rejects_completed_status():
    from docsift.core.config import Settings
    doc = _make_document(DocumentStatus.COMPLETED)
    ext = _make_extraction(doc.id)
    session = _FakeSession(doc, ext)
    with pytest.raises(ValueError, match="не находится в статусе"):
        await complete_document_review(session, str(doc.id), Settings())
    assert not session.committed
    assert doc.status is DocumentStatus.COMPLETED


@pytest.mark.asyncio
async def test_complete_review_succeeds_for_review_required():
    from docsift.core.config import Settings
    doc = _make_document(DocumentStatus.REVIEW_REQUIRED)
    ext = _make_extraction(doc.id)
    session = _FakeSession(doc, ext)
    result = await complete_document_review(session, str(doc.id), Settings())
    assert session.committed
    assert result["completed"] is True
    assert doc.status is DocumentStatus.COMPLETED
