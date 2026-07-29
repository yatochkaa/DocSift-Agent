"""Сценарий загрузки: пользователь должен видеть результат на каждом шаге.

Прежний поток обрывался дважды. Файл уходил на сервер по факту выбора, а
ответ вставлялся внутрь модального окна — стоило его закрыть, и от загрузки
не оставалось следа. Затем первый же тик поллинга подменял всю карточку одним
чипом статуса: пропадали имя файла, ссылка на документ и сам `hx-get`, то есть
опрос прекращался после одного тика.

Здесь проверяется, что каждое состояние доезжает до разметки целиком и
человеческим текстом. Реальный LLM и боевая БД не участвуют — только фейковый
шлюз из conftest.
"""

from __future__ import annotations

import re

import pytest

from .conftest import UnsupportedContentTypeError, UploadTooLargeError

pytestmark = pytest.mark.asyncio

HX = {"HX-Request": "true"}
PDF = {"file": ("doc_08_upd.pdf", b"%PDF-1.4 test", "application/pdf")}

# Ключи из DocumentStatus, которые не должны утечь в интерфейс сырыми.
RAW_STATUS_KEYS = ("review_required", "succeeded", "uploaded", "extracted", "processing")


def visible_text(html: str) -> str:
    """Только то, что пользователь читает: без тегов и их атрибутов.

    data-status="uploaded" — машинная разметка для CSS и JS, она к сырым
    ключам в интерфейсе отношения не имеет.
    """
    return re.sub(r"<[^>]*>", " ", html)


async def _csrf(client) -> str:
    """Токен со страницы списка; cookie клиент сохраняет сам."""
    page = await client.get("/documents")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "на странице нет поля csrf_token"
    return match.group(1)


async def _upload(client, files=PDF) -> object:
    token = await _csrf(client)
    return await client.post(
        "/documents/upload", headers=HX, files=files, data={"csrf_token": token}
    )


# --- 1. Ответ вообще виден ---------------------------------------------------
async def test_upload_returns_visible_card(upload_client):
    client, _ = upload_client
    response = await _upload(client)

    assert response.status_code == 201
    body = response.text
    assert "data-upload-card" in body
    assert 'id="upload-card"' in body
    # Фрагмент, а не страница целиком.
    assert "<html" not in body


# --- 2. Имя файла и понятный начальный статус --------------------------------
async def test_card_shows_file_name_and_human_start_status(upload_client):
    client, gateway = upload_client
    gateway.upload_result = {"id": "doc-new", "status": "uploaded"}

    body = (await _upload(client)).text

    assert "doc_08_upd.pdf" in body
    assert "Файл загружен" in body
    assert "Начинаем обработку" in body
    # Размер и тип видны пользователю, а не только браузеру.
    assert "PDF" in body
    text = visible_text(body)
    for key in RAW_STATUS_KEYS:
        assert key not in text


# --- 3. PROCESSING как «Извлекаем данные» ------------------------------------
async def test_processing_is_shown_as_extracting(upload_client):
    client, gateway = upload_client
    gateway.upload_result = {"id": "doc-new", "status": "processing"}

    body = (await _upload(client)).text

    assert "Извлекаем данные" in body
    assert "processing" not in visible_text(body)
    # Пока обработка идёт, карточка продолжает опрашивать себя.
    assert 'hx-get="/partials/uploads/doc-new"' in body
    assert 'hx-target="this"' in body


# --- 4. REVIEW_REQUIRED --------------------------------------------------------
async def test_review_required_shows_ready_for_review(upload_client):
    client, gateway = upload_client
    gateway.document_status = "review_required"

    response = await client.get("/partials/uploads/doc-new", headers=HX)

    assert response.status_code == 200
    body = response.text
    assert "Документ готов к проверке" in body
    assert "Требуется проверка" in body
    assert "review_required" not in visible_text(body)


# --- 5. Кнопка ведёт на страницу проверки ------------------------------------
async def test_review_button_points_to_review_page(upload_client):
    client, gateway = upload_client
    gateway.document_status = "review_required"

    body = (await client.get("/partials/uploads/doc-new", headers=HX)).text

    assert 'href="/documents/doc-new/review"' in body
    assert "Перейти к проверке" in body


async def test_completed_offers_open_document(upload_client):
    client, gateway = upload_client
    gateway.document_status = "completed"

    body = (await client.get("/partials/uploads/doc-new", headers=HX)).text

    assert "Документ успешно обработан" in body
    assert "Открыть документ" in body
    assert 'href="/documents/doc-new/review"' in body


# --- 6. FAILED показывает понятную ошибку ------------------------------------
async def test_failed_shows_safe_message(upload_client):
    client, gateway = upload_client
    gateway.document_status = "failed"
    gateway.document_error_code = "text_extraction_failed"

    body = (await client.get("/partials/uploads/doc-new", headers=HX)).text

    assert "Не удалось обработать документ" in body
    assert "Не удалось прочитать текст из файла" in body
    assert "Открыть подробности" in body
    # Ни трейсбека, ни сырого кода ошибки, ни JSON.
    assert "Traceback" not in body
    assert "text_extraction_failed" not in body
    assert '{"' not in body
    # Обработка кончилась — опрос прекращается.
    assert "hx-get" not in body


async def test_rejected_upload_explains_reason(upload_client):
    """Отказ приёма приходит настоящим кодом и с человеческим текстом."""
    client, gateway = upload_client
    gateway.upload_error = UploadTooLargeError("payload 41943040 > 20971520")

    response = await _upload(client)

    assert response.status_code == 413
    body = response.text
    assert "Файл больше допустимого размера" in body
    assert "41943040" not in body
    assert "doc_08_upd.pdf" in body

    gateway.upload_error = UnsupportedContentTypeError(".docx")
    other = await _upload(client)
    assert other.status_code == 415
    assert "Такой тип файла не поддерживается" in other.text


# --- 7. Дубликат ---------------------------------------------------------------
async def test_duplicate_links_to_existing_document(upload_client):
    client, gateway = upload_client
    gateway.upload_result = {
        "id": "doc-existing",
        "status": "completed",
        "already_existed": True,
    }

    response = await _upload(client)

    assert response.status_code == 200
    body = response.text
    assert "Этот документ уже был загружен" in body
    assert 'href="/documents/doc-existing/review"' in body
    # Повторная обработка не изображается: опроса нет.
    assert "hx-get" not in body
    assert "Повторная обработка не запускалась" in body


# --- 8. Поллинг подменяет карточку, а не плодит её ----------------------------
async def test_polling_replaces_card_without_duplicates(upload_client):
    client, gateway = upload_client
    gateway.document_status = "processing"

    first = await client.get("/partials/uploads/doc-new", headers=HX)
    second = await client.get("/partials/uploads/doc-new", headers=HX)

    for response in (first, second):
        assert response.status_code == 200
        body = response.text
        # Ровно одна карточка на ответ.
        assert body.count("data-upload-card") == 1
        # Подменяет саму себя, а не добавляется рядом.
        assert 'hx-target="this"' in body
        assert 'hx-swap="outerHTML"' in body
        # И несёт всё, что было в исходной карточке.
        assert "invoice-1.pdf" in body
        assert 'href="/documents/doc-new/review"' in body

    gateway.document_status = "completed"
    final = await client.get("/partials/uploads/doc-new", headers=HX)
    assert final.text.count("data-upload-card") == 1
    assert "hx-get" not in final.text


async def test_polling_route_404_for_unknown_document(upload_client):
    client, _ = upload_client
    response = await client.get("/partials/uploads/missing", headers=HX)
    assert response.status_code == 404


# --- 9. CSRF -------------------------------------------------------------------
async def test_csrf_still_enforced(upload_client):
    client, _ = upload_client

    forged = await client.post(
        "/documents/upload", headers=HX, files=PDF, data={"csrf_token": "поддельный"}
    )
    assert forged.status_code == 403

    accepted = await _upload(client)
    assert accepted.status_code == 201


async def test_forged_non_ascii_token_is_rejected_not_crashed(upload_client):
    """Подделка с кириллицей при выданной cookie — это 403, а не 500.

    hmac.compare_digest на str работает только с ASCII. Пока сравнение шло по
    строкам, такой токен ронял обработчик: до сравнения дело доходило лишь
    когда cookie уже выдана, поэтому проверка порядка здесь существенна —
    сначала получаем cookie, только потом подделываем поле формы.
    """
    client, _ = upload_client
    await _csrf(client)  # cookie выдана — короткий путь «нет cookie» не сработает

    response = await client.post(
        "/documents/upload", headers=HX, files=PDF, data={"csrf_token": "поддельный"}
    )

    assert response.status_code == 403


# --- Панель остаётся на странице после закрытия окна --------------------------
async def test_list_page_has_persistent_tracker_outside_modal(upload_client):
    """Карточка вставляется в панель над списком, а не внутрь модального окна."""
    client, _ = upload_client
    body = (await client.get("/documents")).text

    tracker = body.index('id="upload-tracker"')
    overlay = body.index('data-upload-overlay')
    assert tracker < overlay, "панель загрузки не должна лежать внутри модального окна"

    assert 'aria-live="polite"' in body
    assert "Последняя загрузка" in body
    # Цель формы — панель, а не список внутри окна.
    assert 'hx-target="#upload-list"' in body
    assert 'hx-swap="innerHTML"' in body


async def test_form_requires_explicit_submit(upload_client):
    """Файл не улетает по факту выбора: есть кнопка, и до выбора она выключена."""
    client, _ = upload_client
    body = (await client.get("/documents")).text

    assert "Перетащите PDF сюда или выберите файл" in body
    assert "Загрузить документ" in body
    assert re.search(r'id="upload-submit"[^>]*\sdisabled', body), "кнопка должна стартовать выключенной"
    assert "data-upload-picked" in body
    assert "data-upload-clear" in body
    # Ограничения видны рядом с зоной, а не только в сообщении об отказе.
    assert "PDF" in body
    assert "не больше" in body
