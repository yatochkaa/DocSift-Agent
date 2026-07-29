"""Routes for the DocSift work queue."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..app import is_htmx
from ..deps import DataGateway, get_gateway, get_templates
from ..presenters import build_dashboard, build_documents

router = APIRouter(tags=["web"])


def _kpi_series(runs: list[dict[str, Any]], key: str) -> list[float]:
    return [float(run[key]) for run in runs if run.get(key) is not None]


def _dashboard_page(data: dict[str, Any]):
    runs = list(data.get("runs") or [])
    accuracy_series = _kpi_series(runs, "accuracy")
    duration_series = _kpi_series(runs, "duration_seconds")
    cost_series = _kpi_series(runs, "cost")
    return build_dashboard(
        documents_current=int(data.get("documents_current") or 0),
        documents_previous=int(data.get("documents_previous") or 0),
        accuracy_current=accuracy_series[-1] if accuracy_series else None,
        accuracy_previous=accuracy_series[-2] if len(accuracy_series) > 1 else None,
        avg_duration_current=duration_series[-1] if duration_series else None,
        avg_duration_previous=duration_series[-2] if len(duration_series) > 1 else None,
        cost_current=sum(cost_series) if cost_series else None,
        cost_previous=None,
        documents_trend=[float(v) for v in data.get("documents_trend") or []],
        accuracy_trend=accuracy_series,
        duration_trend=duration_series,
        cost_trend=cost_series,
        accuracy_by_run=runs,
        step_duration_totals=data.get("step_duration_totals") or {},
        events=data.get("events") or [],
    )


def _queue_context(data: dict[str, Any]) -> tuple[list[dict[str, Any]], Any]:
    counts = data.get("status_counts") or {}
    queues = [
        {"label": "Нужно проверить", "count": int(counts.get("review_required", 0)), "hint": "требуют решения", "href": "/documents?status=review_required", "tone": "review"},
        {"label": "В обработке", "count": sum(int(counts.get(key, 0)) for key in ("uploaded", "processing", "extracted")), "hint": "OCR и извлечение", "href": "/documents?status=processing", "tone": "processing"},
        {"label": "Готово", "count": int(counts.get("completed", 0)), "hint": "можно выгружать", "href": "/documents?status=completed", "tone": "ready"},
        {"label": "Ошибки", "count": int(counts.get("failed", 0)), "hint": "нужно разобраться", "href": "/documents?status=failed", "tone": "error"},
    ]
    recent_items = data.get("recent_documents") or []
    recent = build_documents(
        items=recent_items,
        total=len(recent_items),
        page=1,
        per_page=5,
        available_types=(),
        available_statuses=(),
    )
    return queues, recent


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    data = await gateway.dashboard()
    page = _dashboard_page(data)
    queues, recent = _queue_context(data)
    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        {"page": page, "queues": queues, "recent": recent, "title": "Входящие"},
    )


@router.get("/partials/dashboard/feed", response_class=HTMLResponse)
async def dashboard_feed(
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    data = await gateway.dashboard()
    page = _dashboard_page(data)
    queues, recent = _queue_context(data)
    template = "partials/events_feed.html" if is_htmx(request) else "pages/dashboard.html"
    return templates.TemplateResponse(
        request,
        template,
        {"page": page, "queues": queues, "recent": recent, "title": "Входящие"},
    )
