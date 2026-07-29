"""Зависимости веб-слоя: сессия БД, шаблоны, источник данных.

Источник данных вынесен в протокол `DataGateway`: в бою это repository поверх
асинхронной сессии, в тестах — фейк без БД (app.state.gateway).

Вся знание о схеме БД живёт в repository.py — здесь только транспорт.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, Sequence

from fastapi import Request
from fastapi.templating import Jinja2Templates


class DataGateway(Protocol):
    """Минимальный контракт чтения, который нужен роутерам."""

    async def dashboard(self) -> dict[str, Any]: ...

    async def documents(self, **kwargs: Any) -> dict[str, Any]: ...

    async def document(self, document_id: str) -> dict[str, Any] | None: ...

    async def runs(self, *, page: int, per_page: int) -> dict[str, Any]: ...

    async def run(self, run_id: str) -> dict[str, Any] | None: ...

    async def run_pair(self, a: str, b: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]: ...

    async def upload(self, file_name: str, payload: bytes) -> dict[str, Any]: ...

    async def save_correction(self, document_id: str, field_path: str, value: str) -> None: ...

    async def save_bulk_corrections(self, document_id: str, corrections: dict[str, str]) -> None: ...

    async def delete_document(self, document_id: str) -> None: ...

    async def complete_review(self, document_id: str, *, confirm_warnings: bool = False) -> dict[str, Any]: ...


class SqlAlchemyGateway:
    """Реальный шлюз: тонкая обёртка над repository с асинхронной сессией."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def _session(self) -> AsyncIterator[Any]:
        async with self._session_factory() as session:
            yield session

    async def dashboard(self) -> dict[str, Any]:
        from . import repository

        async with self._session_factory() as session:
            data = await repository.dashboard_data(session)
            data["events"] = await repository.recent_events(session)
            return data

    async def documents(self, **kwargs: Any) -> dict[str, Any]:
        from . import repository

        async with self._session_factory() as session:
            items, total = await repository.list_documents(session, **kwargs)
            types, statuses = await repository.document_facets(session)
            return {"items": items, "total": total, "types": types, "statuses": statuses}

    async def document(self, document_id: str) -> dict[str, Any] | None:
        from . import repository

        async with self._session_factory() as session:
            return await repository.get_document_detail(session, document_id)

    async def runs(self, *, page: int, per_page: int) -> dict[str, Any]:
        from . import repository

        async with self._session_factory() as session:
            runs, total = await repository.list_runs(session, page=page, per_page=per_page)
            return {"runs": runs, "total": total}

    async def run(self, run_id: str) -> dict[str, Any] | None:
        from . import repository

        async with self._session_factory() as session:
            return await repository.get_run(session, run_id)

    async def run_pair(self, a: str, b: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        from . import repository

        async with self._session_factory() as session:
            return await repository.get_run_pair(session, a, b)

    async def upload(self, file_name: str, payload: bytes) -> dict[str, Any]:
        """Загрузка делегируется существующему пайплайну проекта.

        Модуль ищется лениво: пока точка входа не подключена, остальной интерфейс
        остаётся рабочим, а загрузка отдаёт понятную ошибку.
        """
        try:
            from docsift.pipeline import ingest_document  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - зависит от этапа проекта
            raise RuntimeError(
                "Загрузка документа не подключена: нет docsift.pipeline.ingest_document"
            ) from exc

        return await ingest_document(
            file_name=file_name,
            payload=payload,
            session_factory=self._session_factory,
        )

    async def save_correction(self, document_id: str, field_path: str, value: str) -> None:
        from . import repository

        async with self._session_factory() as session:
            await repository.save_bulk_document_corrections(session, document_id, {field_path: value})

    async def save_bulk_corrections(self, document_id: str, corrections: dict[str, str]) -> None:
        from . import repository

        async with self._session_factory() as session:
            await repository.save_bulk_document_corrections(session, document_id, corrections)

    async def delete_document(self, document_id: str) -> None:
        from docsift.core.config import get_settings
        from docsift.pipeline.storage import DocumentStorage
        from . import repository

        async with self._session_factory() as session:
            object_key = await repository.delete_document(session, document_id)
        storage = DocumentStorage.from_settings(get_settings())
        try:
            storage.resolve(object_key).unlink(missing_ok=True)
        except (OSError, ValueError):
            # The database record is already gone. A leftover content-addressed
            # file is harmless and can be reused by a later identical upload.
            pass

    async def complete_review(self, document_id: str, *, confirm_warnings: bool = False) -> dict[str, Any]:
        from docsift.core.config import get_settings
        from . import repository

        async with self._session_factory() as session:
            return await repository.complete_document_review(
                session, document_id, get_settings(), confirm_warnings=confirm_warnings
            )


def get_gateway(request: Request) -> DataGateway:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise RuntimeError("app.state.gateway не настроен: передайте SqlAlchemyGateway при старте")
    return gateway


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def page_params(page: str | None, per_page: str | None, default_per_page: int = 25) -> tuple[int, int]:
    try:
        page_value = max(1, int(page or 1))
    except ValueError:
        page_value = 1
    try:
        per_page_value = min(100, max(5, int(per_page or default_per_page)))
    except ValueError:
        per_page_value = default_per_page
    return page_value, per_page_value


def sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else []
