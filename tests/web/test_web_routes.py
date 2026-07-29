"""Код 200 и ключевые фрагменты разметки для каждого экрана."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_dashboard_renders_work_queues(client):
    response = await client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Что требует внимания" in body
    assert "Нужно проверить" in body
    assert "В обработке" in body
    assert "Последние документы" in body
    assert 'id="accuracy-chart"' not in body


async def test_dashboard_empty_state(empty_client):
    response = await empty_client.get("/")
    assert response.status_code == 200
    assert "Документов пока нет" in response.text


async def test_documents_list(client):
    response = await client.get("/documents")
    assert response.status_code == 200
    body = response.text
    assert "invoice-1.pdf" in body
    assert 'id="table-region"' in body
    assert "Контрагент" in body


async def test_documents_status_navigation_is_active(client):
    response = await client.get("/documents", params={"status": "completed"})
    assert response.status_code == 200
    body = response.text
    assert 'href="/documents?status=completed"' in body
    assert 'class="is-active"><i class="is-ready"></i>Готово</a>' in body



async def test_documents_empty_state(empty_client):
    response = await empty_client.get("/documents")
    assert response.status_code == 200
    assert "Здесь пока нет документов" in response.text


async def test_document_detail_tabs(client):
    response = await client.get("/documents/doc-1/review")
    assert response.status_code == 200
    body = response.text
    assert "Guardrails" in body
    assert "Трейс" in body
    assert 'id="raw-json"' in body
    assert 'class="ds-source-pdf"' in body
    assert '/documents/doc-1/source' in body
    assert "Редактировать позиции" in body
    assert "Нажмите для редактирования" in body
    assert "Завершить проверку" in body
    assert "Скачать XLSX" in body


async def test_document_detail_404(client):
    response = await client.get("/documents/missing/review", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "Страница не найдена" in response.text


async def test_evals_list(client):
    response = await client.get("/evals")
    assert response.status_code == 200
    assert "invoices-ru" in response.text
    assert "Сравнить" in response.text


async def test_evals_empty(empty_client):
    response = await empty_client.get("/evals")
    assert response.status_code == 200
    assert "Прогонов пока нет" in response.text


async def test_eval_report(client):
    response = await client.get("/evals/run-a")
    assert response.status_code == 200
    body = response.text
    assert "Тайминги по шагам" in body
    assert "wf-bar" in body


async def test_eval_report_404(client):
    response = await client.get("/evals/missing", headers={"accept": "text/html"})
    assert response.status_code == 404


async def test_compare(client):
    response = await client.get("/evals/compare", params={"a": "run-a", "b": "run-b"})
    assert response.status_code == 200
    body = response.text
    assert "Краткий вывод" in body
    assert "Время и стоимость" in body


async def test_eval_report_sample_links_point_to_review_page(client):
    response = await client.get("/evals/run-a")
    assert 'href="/documents/doc-1/review"' in response.text


# ---------------------------------------------------------------------------
# M2 / L1: empty-state tests for processing and failed documents
# ---------------------------------------------------------------------------

async def test_empty_state_processing_shows_check_again(processing_client):
    """Processing without extraction: вижу сообщение о выполнении и кнопку повтора."""
    response = await processing_client.get("/documents/doc-1/review")
    assert response.status_code == 200
    body = response.text
    assert "Извлечение данных ещё выполняется" in body
    assert "Проверить снова" in body


async def test_empty_state_failed_shows_error_no_retry(failed_client):
    """Failed without extraction: вижу ошибку и не вижу фейковой кнопки повтора."""
    response = await failed_client.get("/documents/doc-1/review")
    assert response.status_code == 200
    body = response.text
    assert "Обработка документа завершилась ошибкой" in body
    assert "Проверить снова" not in body


async def test_empty_state_no_bare_href_hash(processing_client):
    """Empty-state action HTML не содержит точного href=\"#\"."""
    import re
    response = await processing_client.get("/documents/doc-1/review")
    body = response.text
    hrefs = re.findall(r'href="([^"]*)"', body)
    for href in hrefs:
        assert href != "#", f"Found bare href='#' in rendered HTML"


async def test_base_accessibility_link_href_main_allowed(processing_client):
    """href=\"#main\" в базовом шаблоне — допустим и не вызывает ложного срабатывания."""
    import re
    response = await processing_client.get("/documents/doc-1/review")
    body = response.text
    hrefs = re.findall(r'href="([^"]*)"', body)
    assert any(h == "#main" for h in hrefs), "Accessibility link href='#main' should be present"
    bare_hashes = [h for h in hrefs if h == "#"]
    assert bare_hashes == [], f"Bare href='#' found: {bare_hashes}"


async def test_document_can_be_deleted_from_review(client):
    import re

    page = await client.get("/documents/doc-1/review")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token
    response = await client.post(
        "/documents/doc-1/delete",
        data={"csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/documents?deleted=1"
    missing = await client.get("/documents/doc-1/review")
    assert missing.status_code == 404
