"""Синхронное файловое хранилище документов."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Метаданная сохранённого файла."""

    object_key: str
    absolute_path: Path
    sha256: str
    size_bytes: int
    content_type: str
    already_existed: bool


class UploadTooLargeError(Exception):
    """Размер файла превышает допустимый лимит."""


class UnsupportedContentTypeError(Exception):
    """Тип файла не поддерживается."""


class DocumentStorage:
    """Локальное файловое хранилище с детерминированными ключами."""

    _SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
        {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    )
    
    # Magic byte signatures for each supported type
    _MAGIC_SIGNATURES = {
        b"%PDF-": ".pdf",
        b"\x89PNG\r\n\x1a\n": ".png",
        b"\xff\xd8\xff": ".jpg",  # Also covers .jpeg
        b"II*\x00": ".tif",      # Also covers .tiff (little-endian)
        b"MM\x00*": ".tif",      # Also covers .tiff (big-endian)
    }

    def __init__(self, root: Path, max_bytes: int) -> None:
        self._root = root
        self._max_bytes = max_bytes

    @classmethod
    def from_settings(cls, settings: object) -> DocumentStorage:
        """Создать хранилище из объекта настроек."""
        return cls(
            root=Path(settings.storage_path),
            max_bytes=int(settings.max_upload_bytes),
        )

    @staticmethod
    def compute_sha256(payload: bytes) -> str:
        """Вычислить SHA-256 хеш байтового содержимого."""
        return hashlib.sha256(payload).hexdigest()

    def build_object_key(self, sha256: str, file_name: str) -> str:
        """Детерминированный ключ вида ``ab/cd/{sha256}{suffix}``."""
        suffix = Path(file_name).suffix.lower()
        return f"{sha256[:2]}/{sha256[2:4]}/{sha256}{suffix}"

    def detect_content_type(self, file_name: str) -> str:
        """Определить MIME-тип по расширению.

        Поддерживаются: pdf, png, jpg, jpeg, tif, tiff.

        Raises:
            UnsupportedContentTypeError: если расширение не в списке.
        """
        suffix = Path(file_name).suffix.lower()
        mapping: dict[str, str] = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }
        content_type = mapping.get(suffix)
        if content_type is None:
            raise UnsupportedContentTypeError(
                f"Расширение «{suffix}» не поддерживается. "
                f"Допустимы: {', '.join(sorted(self._SUPPORTED_EXTENSIONS))}"
            )
        return content_type

    def _validate_magic_bytes(self, file_name: str, payload: bytes) -> None:
        """Проверить сигнатуру файла по magic bytes.

        Проверяет, что содержимое файла соответствует его расширению.
        
        Args:
            file_name: Имя файла с расширением
            payload: Байтовое содержимое файла
            
        Raises:
            UnsupportedContentTypeError: если сигнатура не соответствует расширению
        """
        suffix = Path(file_name).suffix.lower()
        
        # Normalize extensions for comparison
        normalized_suffix = suffix
        if suffix in (".jpg", ".jpeg"):
            normalized_suffix = ".jpg"
        elif suffix in (".tif", ".tiff"):
            normalized_suffix = ".tif"
        
        # Check magic bytes
        matched_signature = None
        for signature, sig_suffix in self._MAGIC_SIGNATURES.items():
            if payload.startswith(signature):
                matched_signature = sig_suffix
                break
        
        if matched_signature is None:
            raise UnsupportedContentTypeError(
                f"Содержимое файла не соответствует ни одному поддерживаемому типу"
            )
        
        # Check that the signature matches the extension
        if normalized_suffix != matched_signature:
            raise UnsupportedContentTypeError(
                f"Содержимое файла (тип {matched_signature}) не соответствует расширению {suffix}"
            )

    def save(
        self,
        *,
        file_name: str,
        payload: bytes,
    ) -> StoredFile:
        """Сохранить файл на диск.

        Проверяет лимит размера и тип содержимого, создаёт подкаталоги,
        пишет атомарно (во временный файл, затем ``os.replace``).
        Если файл с таким object_key уже существует — не перезаписывает.

        Raises:
            UploadTooLargeError: если ``len(payload) > max_bytes``.
            UnsupportedContentTypeError: если расширение не поддерживается или содержимое не соответствует расширению.
        """
        if len(payload) > self._max_bytes:
            raise UploadTooLargeError(
                f"Размер файла ({len(payload)} байт) превышает "
                f"допустимый лимит ({self._max_bytes} байт)"
            )

        content_type = self.detect_content_type(file_name)
        
        # Validate magic bytes before saving
        self._validate_magic_bytes(file_name, payload)
        
        sha256 = self.compute_sha256(payload)
        object_key = self.build_object_key(sha256, file_name)
        destination = self._root / object_key

        already_existed = destination.is_file()
        if already_existed:
            return StoredFile(
                object_key=object_key,
                absolute_path=destination,
                sha256=sha256,
                size_bytes=len(payload),
                content_type=content_type,
                already_existed=True,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{sha256[:8]}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return StoredFile(
            object_key=object_key,
            absolute_path=destination,
            sha256=sha256,
            size_bytes=len(payload),
            content_type=content_type,
            already_existed=False,
        )

    def resolve(self, object_key: str) -> Path:
        """Разрешить object_key в абсолютный путь внутри root.

        Запрещает выход за пределы корневой директории.

        Raises:
            ValueError: если путь выходит за пределы root.
        """
        resolved = (self._root / object_key).resolve()
        root = self._root.resolve()
        # startswith пропускает соседа var/uploads-evil — сравниваем по частям пути.
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"Путь «{object_key}» выходит за пределы корневой директории хранилища"
            )
        return resolved
