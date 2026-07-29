"""Сборка Jinja-окружения и монтирование веба в существующее приложение."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import filters, security, tokens

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
THEMES_DIR = STATIC_DIR / "themes"


def available_themes() -> list[str]:
    """Доступные темы = имена css-файлов в static/themes."""
    if not THEMES_DIR.is_dir():
        return ["default"]
    names = sorted(p.stem for p in THEMES_DIR.glob("*.css"))
    return names or ["default"]


def default_theme() -> str:
    """Тема по умолчанию из DOCSIFT_WEB_THEME, с откатом на default."""
    name = os.getenv("DOCSIFT_WEB_THEME", "default").strip() or "default"
    return name if name in available_themes() else "default"


def upload_limits() -> dict[str, object]:
    """Ограничения загрузки для подсказки в форме.

    Берутся у пайплайна, а не переписываются в вёрстку: подсказка «PDF до
    20 МБ» обязана совпадать с тем, что сервер действительно примет, иначе
    пользователь узнаёт о лимите только по отказу. Если пайплайн не подключён,
    остаются значения по умолчанию — форма продолжает работать.
    """
    extensions = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
    max_bytes = 20 * 1024 * 1024
    try:
        from docsift.core.config import get_settings
        from docsift.pipeline.storage import DocumentStorage

        max_bytes = int(get_settings().max_upload_bytes)
        extensions = tuple(sorted(DocumentStorage._SUPPORTED_EXTENSIONS))
    except Exception:  # pragma: no cover - зависит от этапа проекта
        pass
    return {
        "accept": ",".join(extensions),
        "extensions_text": ", ".join(e.lstrip(".").upper() for e in extensions),
        "max_bytes": max_bytes,
        "max_text": f"{max_bytes // (1024 * 1024)} МБ",
    }


def build_templates() -> Jinja2Templates:
    """Jinja2Templates с автоэкранированием, фильтрами и токенами дизайна."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.autoescape = True
    env.trim_blocks = True
    env.lstrip_blocks = True
    filters.register(env)
    env.globals.update(
        palette=tokens.PALETTE,
        status_label=tokens.status_label,
        status_tone=tokens.status_tone,
        step_label=tokens.step_label,
        step_color=tokens.step_color,
        confidence_tone=tokens.confidence_tone,
        web_theme=default_theme(),
        web_themes=available_themes(),
        upload_limits=upload_limits(),
    )
    return templates


def is_htmx(request: Request) -> bool:
    """HTMX присылает заголовок HX-Request: true."""
    return request.headers.get("HX-Request", "").lower() == "true"


def mount_web(app: FastAPI, prefix: str = "") -> FastAPI:
    """Монтирует статику и роутеры веб-интерфейса в существующее приложение."""
    from .routes import dashboard, documents, evals

    app.state.templates = build_templates()
    app.mount(f"{prefix}/static", StaticFiles(directory=str(STATIC_DIR)), name="web-static")
    for module in (dashboard, documents, evals):
        app.include_router(module.router, prefix=prefix)

    @app.middleware("http")
    async def _csrf(request: Request, call_next):  # noqa: ANN001, ANN202
        """Выдаём CSRF-токен на каждый запрос и закрепляем его cookie."""
        security.ensure_token(request)
        response = await call_next(request)
        security.attach_csrf_cookie(request, response)
        return response

    @app.exception_handler(404)
    async def _not_found(request: Request, exc) -> Response:  # noqa: ANN001
        """404 для веба рендерим страницей, для API отдаём как есть."""
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(
                {"detail": getattr(exc, "detail", "Not Found")}, status_code=404
            )
        templates: Jinja2Templates = request.app.state.templates
        return templates.TemplateResponse(request, "errors/404.html", {"title": "Страница не найдена"}, status_code=404)

    return app


def create_standalone_app() -> FastAPI:
    """Отдельное приложение — удобно для локального запуска и тестов."""
    app = FastAPI(title="DocSift Web")
    return mount_web(app)
