"""HTMX-эндпоинты отдают только фрагмент, без оболочки страницы."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.asyncio

HX = {"HX-Request": "true"}


async def test_documents_fragment_has_no_html_wrapper(client):
    response = await client.get("/documents", headers=HX)
    assert response.status_code == 200
    body = response.text
    assert "<html" not in body
    assert "<body" not in body
    assert 'id="table-region"' in body


async def test_documents_full_page_has_html_wrapper(client):
    response = await client.get("/documents")
    assert "<html" in response.text
    assert 'id="table-region"' in response.text


async def test_same_fragment_reused_in_both_modes(client):
    full = await client.get("/documents")
    fragment = await client.get("/documents", headers=HX)
    assert "invoice-1.pdf" in full.text
    assert "invoice-1.pdf" in fragment.text


async def test_search_query_is_applied_to_fragment(client):
    response = await client.get("/documents", params={"q": "invoice"}, headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text


async def test_evals_fragment(client):
    response = await client.get("/evals", headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="table-region"' in response.text


async def test_dashboard_feed_fragment(client):
    response = await client.get("/partials/dashboard/feed", headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert 'id="events-feed"' in response.text


async def test_document_status_fragment(client):
    response = await client.get("/partials/documents/doc-1/status", headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "chip" in response.text


async def test_upload_requires_csrf(client):
    response = await client.post(
        "/documents/upload",
        headers=HX,
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        data={"csrf_token": "поддельный"},
    )
    assert response.status_code == 403


async def test_status_fragment_shows_real_status_not_dash(client):
    """Роут поллинга передаёт объект chip, а не строковый ключ status.

    Пока шаблон ждал только status, фрагмент отдавал «—» с классом
    chip-neutral, то есть статус после загрузки не обновлялся.
    """
    response = await client.get("/partials/documents/doc-1/status", headers=HX)
    assert response.status_code == 200
    body = response.text
    assert "—" not in body
    assert "Готово" in body
    assert "chip-success" in body
    assert "chip-neutral" not in body


async def test_status_chip_partial_supports_string_status_key():
    """Второй способ вызова того же партиала — строковым ключом status.

    Партиал умеет оба: роут поллинга передаёт готовый объект chip, вызовы из
    шаблонов — строку. Пока поддержки строки не было, чип рисовал «—» вместо
    статуса. Раньше эту ветку задевал роут загрузки; теперь он рендерит
    карточку целиком, поэтому партиал проверяем напрямую.
    """
    from docsift.web.app import build_templates

    html = build_templates().get_template("partials/status_chip.html").render(status="processing")

    assert "Обработка" in html
    assert "chip-accent" in html
    assert "—" not in html


async def test_upload_returns_card_not_bare_chip(client):
    """Ответ загрузки — карточка с именем файла, а не один чип статуса."""
    page = await client.get("/documents")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    # Cookie уже сохранена клиентом после GET — отдельно передавать её не нужно.
    response = await client.post(
        "/documents/upload",
        headers=HX,
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        data={"csrf_token": token},
    )
    assert response.status_code == 201
    body = response.text
    assert "a.pdf" in body
    assert "Извлекаем данные" in body
    assert "data-upload-card" in body
