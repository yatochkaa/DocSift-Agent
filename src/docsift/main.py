from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from docsift.web.deps import SqlAlchemyGateway
from fastapi import FastAPI
from docsift.web.app import mount_web
from docsift.api.routers.documents import router as documents_router
from docsift.api.routers.health import router as health_router
from docsift.core.config import Settings, get_settings
from docsift.db.session import build_engine, build_session_factory

logger = logging.getLogger(__name__)


async def _warmup_llm(settings: Settings) -> None:
    """Прогреть модель Ollama на старте, чтобы первый документ не тормозил.

    Один минимальный запрос с ``num_predict=1``. Ошибка только логируется —
    приложение стартует в любом случае.
    """
    if not settings.llm_warmup:
        return
    try:
        from docsift.services.llm.factory import build_llm_provider
        from docsift.services.llm.providers import OllamaProvider, warmup_ollama_model

        provider = build_llm_provider(settings)
        if isinstance(provider, OllamaProvider):
            await warmup_ollama_model(provider, num_predict=1)
    except Exception:
        logger.warning(
            "Прогрев LLM не удался — приложение продолжает работу",
            exc_info=True,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    engine = build_engine(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        app_settings.storage_path.mkdir(parents=True, exist_ok=True)
        await _warmup_llm(app_settings)
        yield
        await engine.dispose()
        
    app = FastAPI(title=app_settings.app_name, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.session_factory = build_session_factory(engine)
    app.state.gateway = SqlAlchemyGateway(app.state.session_factory)
    app.include_router(health_router)
    app.include_router(documents_router)
    mount_web(app)
    return app


app = create_app()

