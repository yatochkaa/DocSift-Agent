"""Тесты модуля файлового хранилища DocumentStorage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from docsift.pipeline.storage import (
    DocumentStorage,
    StoredFile,
    UnsupportedContentTypeError,
    UploadTooLargeError,
)

# ── magic bytes prefixes ────────────────────────────────────────────────

_PDF = b"%PDF-"
_PNG = b"\x89PNG\r\n\x1a\n"
_JPG = b"\xff\xd8\xff"
_TIF = b"II*\x00"


# ── вспомогательные fixtures ─────────────────────────────────────────────


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture()
def storage(root: Path) -> DocumentStorage:
    return DocumentStorage(root=root, max_bytes=1024)


# ── happy path ───────────────────────────────────────────────────────────


class TestSaveHappyPath:
    def test_file_written_to_disk(self, storage: DocumentStorage, root: Path) -> None:
        payload = _PDF + b"hello world"
        result = storage.save(file_name="doc.pdf", payload=payload)

        assert isinstance(result, StoredFile)
        assert result.absolute_path.is_file()
        assert result.absolute_path.read_bytes() == payload
        assert result.size_bytes == len(payload)
        assert result.content_type == "application/pdf"
        assert result.sha256 == hashlib.sha256(payload).hexdigest()
        assert result.already_existed is False

    def test_file_is_actually_on_disk(self, storage: DocumentStorage, root: Path) -> None:
        payload = _PNG + b"persistent data"
        result = storage.save(file_name="test.png", payload=payload)

        assert os.path.isfile(result.absolute_path)
        assert result.absolute_path.read_bytes() == payload


# ── детерминированность ключа ────────────────────────────────────────────


class TestObjectKeyDeterminism:
    def test_same_content_different_names_same_key(self, storage: DocumentStorage) -> None:
        payload = b"identical content"
        key_a = storage.build_object_key(storage.compute_sha256(payload), "alpha.pdf")
        key_b = storage.build_object_key(storage.compute_sha256(payload), "beta.pdf")

        assert key_a == key_b

    def test_same_content_different_extensions_different_keys(
        self, storage: DocumentStorage
    ) -> None:
        payload = b"same bytes"
        key_pdf = storage.build_object_key(storage.compute_sha256(payload), "doc.pdf")
        key_png = storage.build_object_key(storage.compute_sha256(payload), "doc.png")

        assert key_pdf != key_png
        assert key_pdf.endswith(".pdf")
        assert key_png.endswith(".png")

    def test_key_has_two_level_nesting(self, storage: DocumentStorage) -> None:
        payload = b"test"
        sha = storage.compute_sha256(payload)
        key = storage.build_object_key(sha, "file.pdf")

        parts = key.split("/")
        assert len(parts) == 3
        assert parts[0] == sha[:2]
        assert parts[1] == sha[2:4]
        assert parts[2] == f"{sha}.pdf"


# ── повторное сохранение ─────────────────────────────────────────────────


class TestAlreadyExisted:
    def test_resave_same_bytes_returns_already_existed(self, storage: DocumentStorage) -> None:
        payload = _PDF + b"duplicate content"
        first = storage.save(file_name="a.pdf", payload=payload)
        second = storage.save(file_name="b.pdf", payload=payload)

        assert first.object_key == second.object_key
        assert second.already_existed is True

    def test_resave_does_not_corrupt_file(self, storage: DocumentStorage) -> None:
        payload = _PDF + b"original data"
        storage.save(file_name="c.pdf", payload=payload)
        result = storage.save(file_name="d.pdf", payload=payload)

        assert result.absolute_path.read_bytes() == payload


# ── превышение лимита ────────────────────────────────────────────────────


class TestUploadTooLarge:
    def test_raises_on_oversized_file(self, storage: DocumentStorage) -> None:
        oversized = _PDF + b"x" * 1021  # 5 + 1021 = 1026 > 1024

        with pytest.raises(UploadTooLargeError, match="превышает"):
            storage.save(file_name="big.pdf", payload=oversized)

    def test_exact_limit_passes(self, storage: DocumentStorage) -> None:
        exact = _PDF + b"y" * (1024 - len(_PDF))  # exactly 1024

        result = storage.save(file_name="exact.pdf", payload=exact)
        assert result.size_bytes == 1024


# ── неподдерживаемый тип ─────────────────────────────────────────────────


class TestUnsupportedContentType:
    def test_exe_rejected(self, storage: DocumentStorage) -> None:
        with pytest.raises(UnsupportedContentTypeError, match="не поддерживается"):
            storage.save(file_name="virus.exe", payload=b"bad")

    def test_txt_rejected(self, storage: DocumentStorage) -> None:
        with pytest.raises(UnsupportedContentTypeError):
            storage.save(file_name="readme.txt", payload=b"text")

    def test_no_extension_rejected(self, storage: DocumentStorage) -> None:
        with pytest.raises(UnsupportedContentTypeError):
            storage.save(file_name="noext", payload=b"data")


# ── resolve и защита от path traversal ────────────────────────────────────


class TestResolve:
    def test_resolve_valid_key(self, storage: DocumentStorage) -> None:
        key = "ab/cd/abcdef0123456789.pdf"
        result = storage.resolve(key)

        assert isinstance(result, Path)
        assert result.name == "abcdef0123456789.pdf"
        assert result.parent.name == "cd"

    def test_resolve_traversal_raises(self, storage: DocumentStorage) -> None:
        with pytest.raises(ValueError, match="выходит за пределы"):
            storage.resolve("../../etc/passwd")


# ──.supported extensions ─────────────────────────────────────────────────


class TestSupportedExtensions:
    @pytest.mark.parametrize(
        "ext,expected_ct,prefix",
        [
            (".pdf", "application/pdf", _PDF),
            (".png", "image/png", _PNG),
            (".jpg", "image/jpeg", _JPG),
            (".jpeg", "image/jpeg", _JPG),
            (".tif", "image/tiff", _TIF),
            (".tiff", "image/tiff", _TIF),
        ],
    )
    def test_supported_types(
        self, storage: DocumentStorage, ext: str, expected_ct: str, prefix: bytes
    ) -> None:
        result = storage.save(file_name=f"doc{ext}", payload=prefix + b"data")
        assert result.content_type == expected_ct

    def test_uppercase_extension_normalised(self, storage: DocumentStorage) -> None:
        result = storage.save(file_name="DOC.PDF", payload=_PDF + b"data")
        assert result.content_type == "application/pdf"
        assert result.object_key.endswith(".pdf")
