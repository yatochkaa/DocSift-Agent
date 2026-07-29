"""Роуты прогонов evals: список, отчёт, сравнение."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..app import is_htmx
from ..deps import DataGateway, get_gateway, get_templates, page_params
from ..presenters import build_compare, build_eval_report, build_evals

router = APIRouter(tags=["web"])


@router.get("/evals", response_class=HTMLResponse)
async def evals_list(
    request: Request,
    page: str | None = None,
    per_page: str | None = None,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    page_num, per_page_num = page_params(page, per_page, default_per_page=20)
    data = await gateway.runs(page=page_num, per_page=per_page_num)
    view = build_evals(runs=data["runs"], total=data["total"], page=page_num, per_page=per_page_num)
    template = "partials/runs_table.html" if is_htmx(request) else "pages/evals_list.html"
    return templates.TemplateResponse(request, template, {"page": view, "title": view.title})


@router.get("/evals/compare", response_class=HTMLResponse)
async def evals_compare(
    request: Request,
    a: str,
    b: str,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    run_a, run_b = await gateway.run_pair(a, b)
    if run_a is None or run_b is None:
        raise HTTPException(status_code=404, detail="Один из прогонов не найден")
    view = build_compare(
        run_a=run_a,
        run_b=run_b,
        metrics_a=run_a.get("metrics") or [],
        metrics_b=run_b.get("metrics") or [],
    )
    return templates.TemplateResponse(request, "pages/evals_compare.html", {"page": view, "title": view.title})


@router.get("/evals/{run_id}", response_class=HTMLResponse)
async def eval_report(
    run_id: str,
    request: Request,
    gateway: DataGateway = Depends(get_gateway),
    templates: Jinja2Templates = Depends(get_templates),
) -> HTMLResponse:
    run = await gateway.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Прогон не найден")
    view = build_eval_report(
        run=run,
        metrics=run.get("metrics") or [],
        samples=run.get("samples") or [],
        step_duration_totals=run.get("step_duration_totals") or {},
        errors=run.get("errors") or [],
    )
    return templates.TemplateResponse(request, "pages/eval_report.html", {"page": view, "title": view.title})
