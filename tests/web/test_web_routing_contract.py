"""Контракт маршрутов веба рядом с API.

API-роутер подключается в main.py раньше веб-роутеров и занимает
``GET /documents/{document_id}`` вместе с обязательным ``X-Tenant-ID``.
Пока страница проверки жила на том же пути, обычный переход по ссылке
получал 422 и до веб-обработчика не доходил.

Тесты веба используют ``create_standalone_app()`` — там API-роутера нет,
поэтому коллизию они поймать не могли. Здесь приложение собирается так же,
как в main.py, но без настроек и БД.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from docsift.api.dependencies import get_document_service
from docsift.api.routers.documents import router as api_documents_router
from docsift.web.app import mount_web

from .conftest import FakeGateway

pytestmark = pytest.mark.asyncio


def _composed_app() -> FastAPI:
    """Порядок подключения как в main.py: сначала API, затем веб."""
    app = FastAPI()
    app.include_router(api_documents_router)
    mount_web(app)
    app.state.gateway = FakeGateway()
    # Реальная зависимость открывает сессию БД; для проверки заголовка она не нужна.
    app.dependency_overrides[get_document_service] = lambda: object()
    return app


@pytest_asyncio.fixture
async def composed_client():
    async with AsyncClient(
        transport=ASGITransport(app=_composed_app()), base_url="http://test"
    ) as ac:
        yield ac


async def test_detail_opens_without_tenant_header(composed_client):
    """Обычный переход браузера по ссылке: заголовков нет, страница есть."""
    response = await composed_client.get("/documents/doc-1/review")
    assert response.status_code == 200
    assert "X-Tenant-ID" not in response.text


async def test_detail_shows_selected_document(composed_client):
    response = await composed_client.get("/documents/doc-1/review")
    assert response.status_code == 200
    assert "invoice-1.pdf" in response.text


async def test_link_from_list_is_reachable(composed_client):
    """Ссылку берём из разметки списка и открываем как браузер."""
    listing = await composed_client.get("/documents")
    assert listing.status_code == 200

    hrefs = re.findall(r'href="(/documents/[^"]+/review)"', listing.text)
    assert hrefs, "в списке нет ссылки на страницу проверки"

    followed = await composed_client.get(hrefs[0])
    assert followed.status_code == 200


async def test_missing_document_still_404(composed_client):
    response = await composed_client.get(
        "/documents/missing/review", headers={"accept": "text/html"}
    )
    assert response.status_code == 404


async def test_api_route_still_requires_tenant_header(composed_client):
    """Проверку тенанта у API не ослабляли: без заголовка по-прежнему 422."""
    response = await composed_client.get(f"/documents/{uuid4()}")
    assert response.status_code == 422
    assert any(
        error.get("loc") == ["header", "X-Tenant-ID"]
        for error in response.json()["detail"]
    )


async def test_web_and_api_paths_do_not_collide():
    """Страница проверки не должна перекрывать путь API и наоборот."""
    app = _composed_app()
    paths = set()
    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        for sub in original.routes:
            methods = getattr(sub, "methods", None) or set()
            if "GET" in methods:
                paths.add(getattr(original, "prefix", "") + sub.path
                          if not sub.path.startswith("/documents")
                          else sub.path)

    assert "/documents/{document_id}" in paths  # API
    assert "/documents/{document_id}/review" in paths  # веб
