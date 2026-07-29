"""Роуты списка документов, карточки и загрузки."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote

from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from ..app import is_htmx
from ..deps import DataGateway, get_gateway, get_templates, page_params
from ..presenters import build_document_detail, build_documents, build_upload_card, card_from_document
from ..security import verify_csrf

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])


@router.get("/documents", response_class=HTMLResponse)
async def documents_list(
    request: Request,
    q: str = "",
    type: str = "",  # noqa: A002 - имя параметра в URL
    status: str = "",
    sort: str = "uploaded_at",
    dir: str = "desc",  # noqa: A002
    page: str | None = None,
    per_page: str | None = None,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Один и тот же обработчик отдаёт страницу или фрагмент таблицы."""
    page_num, per_page_num = page_params(page, per_page)
    data = await gateway.documents(
        query=q,
        doc_type=type,
        status=status,
        sort=sort,
        direction=dir,
        page=page_num,
        per_page=per_page_num,
    )
    view = build_documents(
        items=data["items"],
        total=data["total"],
        page=page_num,
        per_page=per_page_num,
        query=q,
        doc_type=type,
        status=status,
        sort=sort,
        direction=dir,
        available_types=data.get("types", []),
        available_statuses=data.get("statuses", []),
    )
    template = "partials/documents_table.html" if is_htmx(request) else "pages/documents_list.html"
    return templates.TemplateResponse(request, template, {"page": view, "title": view.title})


# Путь заканчивается на /review не для красоты: API-роутер подключается в
# main.py раньше веба и занимает ровно `GET /documents/{document_id}` вместе с
# обязательным заголовком X-Tenant-ID. Совпадающий путь веба до обработчика не
# доходил — браузер получал 422. Отдельный сегмент снимает коллизию, не трогая
# API и его проверку тенанта: веб остаётся на общем для списка и загрузки
# контракте «тенант выбирает сервер, заголовок не нужен».
@router.get("/documents/{document_id}/review", response_class=HTMLResponse)
async def document_detail(
    document_id: str,
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    raw = await gateway.document(document_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    view = build_document_detail(
        document=raw["document"],
        extracted=raw.get("extracted") or {},
        guardrails=raw.get("guardrails") or [],
        step_durations=raw.get("step_durations") or {},
        pages=raw.get("pages") or [],
        review=raw.get("review") or {},
    )
    return templates.TemplateResponse(request, "pages/document_detail.html", {"page": view, "title": view.title})


@router.post(
    "/documents/{document_id}/fields",
    dependencies=[Depends(verify_csrf)],
)
async def save_document_field(
    document_id: str,
    field_path: str = Form(...),
    value: str = Form(""),
    gateway: DataGateway = Depends(get_gateway),
) -> RedirectResponse:
    try:
        await gateway.save_correction(document_id, field_path, value)
    except ValueError as exc:
        message = quote(str(exc), safe="")
        return RedirectResponse(
            f"/documents/{document_id}/review?review_error={message}",
            status_code=303,
        )
    return RedirectResponse(
        f"/documents/{document_id}/review?field_saved=1",
        status_code=303,
    )


@router.post(
    "/documents/{document_id}/bulk-edit",
    dependencies=[Depends(verify_csrf)],
)
async def save_bulk_corrections(
    document_id: str,
    paths: List[str] = Form(...),
    values: List[str] = Form(...),
    gateway: DataGateway = Depends(get_gateway),
) -> RedirectResponse:
    """Проверить и атомарно сохранить изменения позиций."""
    try:
        if len(paths) != len(values):
            raise ValueError("Некорректный набор изменений")
        corrections = dict(zip(paths, values))
        await gateway.save_bulk_corrections(document_id, corrections)
        return RedirectResponse(
            f"/documents/{document_id}/review?bulk_saved=1", status_code=303
        )
    except ValueError as exc:
        message = quote(str(exc), safe="")
        return RedirectResponse(
            f"/documents/{document_id}/review?review_error={message}", status_code=303
        )


@router.post(
    "/documents/{document_id}/complete",
    dependencies=[Depends(verify_csrf)],
)
async def finish_document_review(
    document_id: str,
    confirm_warnings: bool = Form(False),
    gateway: DataGateway = Depends(get_gateway),
) -> RedirectResponse:
    try:
        result = await gateway.complete_review(document_id, confirm_warnings=confirm_warnings)
    except ValueError as exc:
        message = quote(str(exc), safe="")
        return RedirectResponse(
            f"/documents/{document_id}/review?review_error={message}",
            status_code=303,
        )

    if result.get("completed"):
        query = "review_complete=1"
        if result.get("warnings_confirmed"):
            query += "&warnings_confirmed=1"
    elif result.get("requires_confirmation"):
        query = "requires_confirmation=1"
    else:
        query = f"review_issues={result.get('issues', 0)}"

    return RedirectResponse(f"/documents/{document_id}/review?{query}", status_code=303)


@router.post(
    "/documents/{document_id}/delete",
    dependencies=[Depends(verify_csrf)],
)
async def delete_document(
    document_id: str,
    gateway: DataGateway = Depends(get_gateway),
) -> RedirectResponse:
    try:
        await gateway.delete_document(document_id)
    except ValueError as exc:
        message = quote(str(exc), safe="")
        return RedirectResponse(
            f"/documents/{document_id}/review?review_error={message}", status_code=303
        )
    return RedirectResponse("/documents?deleted=1", status_code=303)


@router.get("/documents/{document_id}/export.xlsx")
async def export_document_xlsx(
    document_id: str,
    gateway: DataGateway = Depends(get_gateway),
) -> StreamingResponse:
    payload = await gateway.document(document_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    if str(payload["document"].get("status") or "") != "completed":
        raise HTTPException(status_code=409, detail="Сначала завершите проверку документа")
    from ..export_xlsx import build_document_xlsx

    content = build_document_xlsx(payload)
    stem = str(payload["document"].get("file_name") or "document").rsplit(".", 1)[0]
    filename = f"docsift_{stem}.xlsx"
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/documents/{document_id}/source")
async def document_source(
    document_id: str,
    gateway: DataGateway = Depends(get_gateway),
):
    """Serve the stored original through a safe, document-scoped URL."""
    raw = await gateway.document(document_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    document = dict(raw["document"])
    object_key = str(document.get("object_key") or "")
    if not object_key:
        raise HTTPException(status_code=404, detail="Исходный файл не найден")
    from docsift.core.config import get_settings
    from docsift.pipeline.storage import DocumentStorage

    storage = DocumentStorage.from_settings(get_settings())
    try:
        path = storage.resolve(object_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Исходный файл не найден") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Исходный файл не найден")
    
    # Determine media_type from server-side allowlist based on file extension, not from database
    file_name = str(document.get("file_name") or path.name)
    try:
        media_type = storage.detect_content_type(file_name)
    except Exception:
        media_type = "application/octet-stream"
    
    # Add security headers to prevent XSS and content sniffing
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "X-Frame-Options": "SAMEORIGIN",
    }
    
    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=file_name,
        content_disposition_type="inline",
        headers=headers,
    )


@router.get("/partials/documents/{document_id}/status", response_class=HTMLResponse)
async def document_status(
    document_id: str,
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Поллинг статуса строки в таблице: нужен именно чип, без обвязки."""
    raw = await gateway.document(document_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    from ..presenters.common import status_chip

    return templates.TemplateResponse(
        request,
        "partials/status_chip.html",
        {"chip": status_chip(raw["document"].get("status")), "document_id": document_id},
    )


@router.get("/partials/uploads/{document_id}", response_class=HTMLResponse)
async def upload_card(
    document_id: str,
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Тик поллинга карточки загрузки.

    Отдаёт карточку целиком, а не один чип: карточка подменяет сама себя
    (`hx-swap="outerHTML"`), поэтому фрагмент обязан нести и имя файла, и
    ссылку, и следующий `hx-get`. Прежний вариант ходил в соседний роут со
    статусом и получал один `<span>` — карточка схлопывалась в него после
    первого же тика, а поллинг вместе с ней прекращался.
    """
    raw = await gateway.document(document_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    document = dict(raw["document"])
    return templates.TemplateResponse(
        request,
        "partials/upload_item.html",
        {"card": card_from_document(document, error_code=document.get("error_code"))},
    )


def _upload_error_code(exc: Exception) -> str:
    """Код ошибки приёма файла — по нему tokens.py выдаёт текст пользователю.

    Классы опознаём по имени, а не импортом из пайплайна: веб работает через
    протокол DataGateway и не должен зависеть от того, какой шлюз подключён —
    боевой поверх пайплайна или фейковый в тестах.
    """
    return {
        "UploadTooLargeError": "upload_too_large",
        "FileTooLargeError": "upload_too_large",
        "UnsupportedContentTypeError": "unsupported_content_type",
        "UnsupportedFileTypeError": "unsupported_content_type",
    }.get(type(exc).__name__, "internal_error")


UPLOAD_ERROR_STATUS = {
    "upload_too_large": 413,
    "unsupported_content_type": 415,
    "internal_error": 500,
}


@router.post("/documents/upload", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def upload_document(
    request: Request,
    file: UploadFile,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    """Приём файла. Отвечает карточкой всегда — в том числе при отказе.

    Отказ уходит с настоящим кодом (413/415/500), а не замаскированным под
    200. Чтобы пользователь при этом видел причину, а не пустой экран, подмена
    на ошибке разрешена точечно в app.js (htmx:beforeSwap).
    """
    from docsift.core.config import get_settings

    settings = get_settings()
    file_name = file.filename or "document.pdf"
    
    # Check Content-Length header first if present to avoid reading oversized files
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_upload_bytes:
                payload = b""  # Don't read the body
                code = "upload_too_large"
                logger.warning("upload rejected: Content-Length %s > %s", content_length, settings.max_upload_bytes)
                card = build_upload_card(
                    document_id=None,
                    file_name=file_name,
                    status="failed",
                    size_bytes=int(content_length),
                    content_type=file.content_type,
                    error_code=code,
                )
                return templates.TemplateResponse(
                    request,
                    "partials/upload_item.html",
                    {"card": card},
                    status_code=UPLOAD_ERROR_STATUS[code],
                )
        except ValueError:
            # Invalid Content-Length header, continue to read the file
            pass
    
    # Read file in chunks to avoid DoS via memory exhaustion
    payload = bytearray()
    chunk_size = settings.upload_chunk_bytes
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        payload.extend(chunk)
        # Stop reading if we exceed the limit
        if len(payload) > settings.max_upload_bytes:
            from docsift.pipeline.storage import UploadTooLargeError
            raise UploadTooLargeError(
                f"Размер файла ({len(payload)} байт) превышает "
                f"допустимый лимит ({settings.max_upload_bytes} байт)"
            )
    
    payload = bytes(payload)

    try:
        result = await gateway.upload(file_name, payload)
    except Exception as exc:  # noqa: BLE001 - наружу уходит текст по коду, не тип
        code = _upload_error_code(exc)
        logger.warning("upload rejected: %s -> %s", type(exc).__name__, code)
        card = build_upload_card(
            document_id=None,
            file_name=file_name,
            status="failed",
            size_bytes=len(payload),
            content_type=file.content_type,
            error_code=code,
        )
        return templates.TemplateResponse(
            request,
            "partials/upload_item.html",
            {"card": card},
            status_code=UPLOAD_ERROR_STATUS[code],
        )

    # already_existed приходит из ingest_document и раньше просто терялся —
    # повторная загрузка выглядела как новая обработка, которой не было.
    card = build_upload_card(
        document_id=result.get("id"),
        file_name=file_name,
        status=result.get("status"),
        size_bytes=len(payload),
        content_type=file.content_type,
        uploaded_at=datetime.now(timezone.utc),
        already_existed=bool(result.get("already_existed")),
    )
    return templates.TemplateResponse(
        request,
        "partials/upload_item.html",
        {"card": card},
        status_code=200 if card.is_duplicate else 201,
    )