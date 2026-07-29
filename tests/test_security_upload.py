"""Storage-level security tests for upload and file serving vulnerabilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from docsift.pipeline.storage import DocumentStorage, UnsupportedContentTypeError


# Test 3: PDF file with non-PDF content rejected
async def test_pdf_with_non_pdf_content_rejected(tmp_path: Path):
    """File with .pdf extension but non-PDF magic bytes should be rejected."""
    storage = DocumentStorage(root=tmp_path, max_bytes=1024 * 1024)

    with pytest.raises(UnsupportedContentTypeError, match="не соответствует расширению"):
        storage.save(file_name="test.pdf", payload=b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    assert len(list(tmp_path.rglob("*.pdf"))) == 0


# Test 4: PNG file with PDF content rejected
async def test_png_with_pdf_content_rejected(tmp_path: Path):
    """File with .png extension but PDF magic bytes should be rejected."""
    storage = DocumentStorage(root=tmp_path, max_bytes=1024 * 1024)

    with pytest.raises(UnsupportedContentTypeError, match="не соответствует расширению"):
        storage.save(file_name="test.png", payload=b"%PDF-1.4" + b"x" * 100)

    assert len(list(tmp_path.rglob("*.png"))) == 0


# Test 5: Valid minimal PDF accepted and saved
async def test_valid_minimal_pdf_accepted_and_saved(tmp_path: Path):
    """Valid minimal PDF file should be accepted and saved to disk."""
    storage = DocumentStorage(root=tmp_path, max_bytes=1024 * 1024)

    pdf_content = b"%PDF-1.4\n" + b"x" * 100

    result = storage.save(file_name="test.pdf", payload=pdf_content)

    assert result.content_type == "application/pdf"
    assert result.size_bytes == len(pdf_content)
    assert not result.already_existed

    assert len(list(tmp_path.rglob("*.pdf"))) == 1
    assert result.absolute_path.exists()


# Test 6: Valid minimal PNG accepted and saved
async def test_valid_minimal_png_accepted_and_saved(tmp_path: Path):
    """Valid minimal PNG file should be accepted and saved to disk."""
    storage = DocumentStorage(root=tmp_path, max_bytes=1024 * 1024)

    png_content = b"\x89PNG\r\n\x1a\n" + b"x" * 100

    result = storage.save(file_name="test.png", payload=png_content)

    assert result.content_type == "image/png"
    assert result.size_bytes == len(png_content)
    assert not result.already_existed

    assert len(list(tmp_path.rglob("*.png"))) == 1
    assert result.absolute_path.exists()
