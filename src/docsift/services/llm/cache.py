from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.text_extraction import TextExtractionResult

CACHE_SCHEMA_VERSION = "1"


def content_hash(text_result: TextExtractionResult) -> str:
    """Детерминированный хеш содержимого документа.

    Хешируется именно содержимое (медиа-тип, страницы, блоки, таблицы), а не
    путь к файлу: два разных файла с одинаковым текстом должны делить запись
    кеша. Поля вроде ``source_path`` в хеш не входят.
    """
    payload = {
        "media_type": text_result.media_type,
        "used_ocr": text_result.used_ocr,
        "pages": [page.model_dump(mode="json") for page in text_result.pages],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Ключ кеша: содержимое + версия промта + модель.

    Смена любого из трёх компонентов даёт новый ключ, то есть старый результат
    становится невалидным автоматически.
    """

    content_hash: str
    prompt_version: str
    model: str
    schema_version: str = CACHE_SCHEMA_VERSION

    def __str__(self) -> str:
        return f"{self.schema_version}:{self.prompt_version}:{self.model}:{self.content_hash}"


@dataclass(slots=True)
class CacheEntry:
    """Сохранённый результат извлечения с аудитом стоимости/времени.

    При попадании в кеш eval-отчёт показывает нулевые токены и стоимость, но
    ненулевое время (дешёвая операция), что позволяет отличить кеш-попадание от
    реального вызова модели.
    """

    document: ExtractedDocument
    model: str
    prompt_version: str
    content_hash: str
    created_at: float = field(default=0.0)
    hits: int = 0


class ExtractionCacheProtocol(Protocol):
    """Общий интерфейс кешей извлечения.

    Сервисам всё равно, в памяти лежат записи или на диске.
    """

    def make_key(
        self,
        text_result: TextExtractionResult,
        prompt_version: str,
        model: str,
    ) -> CacheKey: ...

    def get(self, key: CacheKey) -> CacheEntry | None: ...

    def store(
        self,
        key: CacheKey,
        document: ExtractedDocument,
        *,
        created_at: float = 0.0,
    ) -> CacheEntry: ...

    def stats(self) -> dict[str, Any]: ...

    def clear(self) -> None: ...


class ExtractionCache:
    """In-memory кеш результатов извлечения.

    Потокобезопасность в рамках одного процесса обеспечивается GIL для
    операций чтения/записи dict; для межпроцессных воркеров кеш не делится —
    это намеренно, eval-прогоны и так выполняются в одном процессе.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def make_key(
        self,
        text_result: TextExtractionResult,
        prompt_version: str,
        model: str,
    ) -> CacheKey:
        return CacheKey(
            content_hash=content_hash(text_result),
            prompt_version=prompt_version,
            model=model,
        )

    def get(self, key: CacheKey) -> CacheEntry | None:
        entry = self._entries.get(str(key))
        if entry is None:
            self._misses += 1
            return None
        entry.hits += 1
        self._hits += 1
        return entry

    def store(self, key: CacheKey, document: ExtractedDocument, *, created_at: float = 0.0) -> CacheEntry:
        if self._max_entries is not None and len(self._entries) >= self._max_entries:
            # Простой FIFO-вытеснение: удаляем самую старую запись.
            oldest_key = next(iter(self._entries))
            self._entries.pop(oldest_key, None)
        entry = CacheEntry(
            document=document,
            model=key.model,
            prompt_version=key.prompt_version,
            content_hash=key.content_hash,
            created_at=created_at,
        )
        self._entries[str(key)] = entry
        return entry

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
        }

    def clear(self) -> None:
        self._entries.clear()
        self._hits = 0
        self._misses = 0


class DiskExtractionCache:
    """Кеш результатов извлечения на диске.

    Отличается от :class:`ExtractionCache` только хранилищем: тот живёт в памяти
    процесса и умирает вместе с ним, а этот кладёт каждую запись в отдельный JSON.
    Для eval-прогонов это принципиально: каждый ``python -m eval.run`` — новый
    процесс, и без диска ответ модели приходится ждать заново.

    Имя файла — sha256 от строкового ключа, а не сам ключ: в ключ входит имя модели
    вроде ``qwen2.5-coder:7b``, а двоеточие в именах файлов на Windows запрещено.
    Полный ключ дублируется внутри файла и сверяется при чтении.

    Повреждённая запись никогда не валит прогон: она считается промахом и удаляется,
    после чего модель спрашивается заново. Кеш — ускорение, а не источник истины.
    """

    DEFAULT_DIRECTORY = Path("var/llm-cache")

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = Path(directory) if directory is not None else self.DEFAULT_DIRECTORY
        self._hits = 0
        self._misses = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def make_key(
        self,
        text_result: TextExtractionResult,
        prompt_version: str,
        model: str,
    ) -> CacheKey:
        return CacheKey(
            content_hash=content_hash(text_result),
            prompt_version=prompt_version,
            model=model,
        )

    def path_for(self, key: CacheKey) -> Path:
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"

    def get(self, key: CacheKey) -> CacheEntry | None:
        path = self.path_for(key)
        if not path.is_file():
            self._misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("key") != str(key):
                raise ValueError("cache key mismatch")
            document = ExtractedDocument.model_validate(payload["document"])
        except Exception:
            # Битый или устаревший по формату файл — считаем промахом и убираем.
            path.unlink(missing_ok=True)
            self._misses += 1
            return None
        hits = int(payload.get("hits", 0)) + 1
        entry = CacheEntry(
            document=document,
            model=payload.get("model", key.model),
            prompt_version=payload.get("prompt_version", key.prompt_version),
            content_hash=payload.get("content_hash", key.content_hash),
            created_at=float(payload.get("created_at", 0.0)),
            hits=hits,
        )
        payload["hits"] = hits
        self._write(path, payload)
        self._hits += 1
        return entry

    def store(
        self,
        key: CacheKey,
        document: ExtractedDocument,
        *,
        created_at: float = 0.0,
    ) -> CacheEntry:
        entry = CacheEntry(
            document=document,
            model=key.model,
            prompt_version=key.prompt_version,
            content_hash=key.content_hash,
            created_at=created_at,
        )
        self._write(
            self.path_for(key),
            {
                "schema_version": key.schema_version,
                "key": str(key),
                "model": key.model,
                "prompt_version": key.prompt_version,
                "content_hash": key.content_hash,
                "created_at": created_at,
                "hits": 0,
                "document": document.model_dump(mode="json"),
            },
        )
        return entry

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        """Атомарная запись: прерванный прогон не оставит половинчатый JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> dict[str, Any]:
        entries = len(list(self._directory.glob("*.json"))) if self._directory.is_dir() else 0
        return {
            "entries": entries,
            "hits": self._hits,
            "misses": self._misses,
            "directory": str(self._directory),
        }

    def clear(self) -> None:
        if self._directory.is_dir():
            for path in self._directory.glob("*.json"):
                path.unlink(missing_ok=True)
        self._hits = 0
        self._misses = 0


class DiskExtractionCache:
    """Кеш результатов извлечения на диске.

    Отличается от :class:`ExtractionCache` только хранилищем: тот живёт в памяти
    процесса и умирает вместе с ним, а этот кладёт каждую запись в отдельный JSON.
    Для eval-прогонов это принципиально: каждый ``python -m eval.run`` — новый
    процесс, и без диска ответ модели приходится ждать заново.

    Имя файла — sha256 от строкового ключа, а не сам ключ: в ключ входит имя
    модели вроде ``qwen2.5-coder:7b``, а двоеточие в именах файлов на Windows
    запрещено. Полный ключ дублируется внутри файла и сверяется при чтении.

    Повреждённая запись никогда не валит прогон: она считается промахом и
    удаляется, после чего модель спрашивается заново. Кеш — ускорение, а не
    источник истины.
    """

    DEFAULT_DIRECTORY = Path("var/llm-cache")

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = Path(directory) if directory is not None else self.DEFAULT_DIRECTORY
        self._hits = 0
        self._misses = 0

    @property
    def directory(self) -> Path:
        return self._directory

    def make_key(
        self,
        text_result: TextExtractionResult,
        prompt_version: str,
        model: str,
    ) -> CacheKey:
        return CacheKey(
            content_hash=content_hash(text_result),
            prompt_version=prompt_version,
            model=model,
        )

    def path_for(self, key: CacheKey) -> Path:
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"

    def get(self, key: CacheKey) -> CacheEntry | None:
        path = self.path_for(key)
        if not path.is_file():
            self._misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("key") != str(key):
                raise ValueError("cache key mismatch")
            document = ExtractedDocument.model_validate(payload["document"])
        except Exception:
            path.unlink(missing_ok=True)
            self._misses += 1
            return None
        hits = int(payload.get("hits", 0)) + 1
        entry = CacheEntry(
            document=document,
            model=payload.get("model", key.model),
            prompt_version=payload.get("prompt_version", key.prompt_version),
            content_hash=payload.get("content_hash", key.content_hash),
            created_at=float(payload.get("created_at", 0.0)),
            hits=hits,
        )
        payload["hits"] = hits
        self._write(path, payload)
        self._hits += 1
        return entry

    def store(
        self,
        key: CacheKey,
        document: ExtractedDocument,
        *,
        created_at: float = 0.0,
    ) -> CacheEntry:
        entry = CacheEntry(
            document=document,
            model=key.model,
            prompt_version=key.prompt_version,
            content_hash=key.content_hash,
            created_at=created_at,
        )
        self._write(
            self.path_for(key),
            {
                "schema_version": key.schema_version,
                "key": str(key),
                "model": key.model,
                "prompt_version": key.prompt_version,
                "content_hash": key.content_hash,
                "created_at": created_at,
                "hits": 0,
                "document": document.model_dump(mode="json"),
            },
        )
        return entry

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        """Атомарная запись: прерванный прогон не оставит половинчатый JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def stats(self) -> dict[str, Any]:
        entries = len(list(self._directory.glob("*.json"))) if self._directory.is_dir() else 0
        return {
            "entries": entries,
            "hits": self._hits,
            "misses": self._misses,
            "directory": str(self._directory),
        }

    def clear(self) -> None:
        if self._directory.is_dir():
            for path in self._directory.glob("*.json"):
                path.unlink(missing_ok=True)
        self._hits = 0
        self._misses = 0
