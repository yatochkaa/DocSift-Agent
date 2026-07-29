"""Презентер дашборда `/`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .. import filters
from ..tokens import PALETTE, step_color, step_label
from .common import Chip, EmptyState, KpiCard, build_sparkline, make_delta


@dataclass(frozen=True)
class ChartSeries:
    """Готовые данные для Chart.js — шаблон только сериализует их в JSON."""

    labels: tuple[str, ...]
    values: tuple[float, ...]
    colors: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class FeedItem:
    kind: str
    icon: str
    title: str
    subtitle: str
    time_text: str
    href: str | None
    chip: Chip | None


@dataclass(frozen=True)
class DashboardPage:
    title: str
    kpis: tuple[KpiCard, ...]
    accuracy_chart: ChartSeries
    steps_chart: ChartSeries
    feed: tuple[FeedItem, ...]
    feed_empty: EmptyState | None
    is_empty: bool


def build_dashboard(
    *,
    documents_current: int,
    documents_previous: int,
    accuracy_current: float | None,
    accuracy_previous: float | None,
    avg_duration_current: float | None,
    avg_duration_previous: float | None,
    cost_current: float | None,
    cost_previous: float | None,
    documents_trend: Sequence[float],
    accuracy_trend: Sequence[float],
    duration_trend: Sequence[float],
    cost_trend: Sequence[float],
    accuracy_by_run: Sequence[Mapping[str, Any]],
    step_duration_totals: Mapping[str, float],
    events: Sequence[Mapping[str, Any]],
) -> DashboardPage:
    """Собирает всё содержимое дашборда. Все аргументы — простые типы."""
    kpis = (
        KpiCard(
            key="documents",
            title="Документов за 30 дней",
            value=filters.number(documents_current),
            hint="обработано",
            delta=make_delta(documents_current, documents_previous, kind="number"),
            sparkline=build_sparkline(documents_trend),
            icon="files",
        ),
        KpiCard(
            key="accuracy",
            title="Точность последнего прогона",
            value=filters.percent(accuracy_current),
            hint="средняя по полям",
            delta=make_delta(accuracy_current, accuracy_previous, kind="percent"),
            sparkline=build_sparkline(accuracy_trend),
            icon="target",
        ),
        KpiCard(
            key="duration",
            title="Средняя длительность",
            value=filters.duration(avg_duration_current),
            hint="на документ",
            delta=make_delta(avg_duration_current, avg_duration_previous, kind="duration", higher_is_better=False),
            sparkline=build_sparkline(duration_trend),
            icon="timer",
        ),
        KpiCard(
            key="cost",
            title="Суммарная стоимость",
            value=filters.usd(cost_current),
            hint="за 30 дней",
            delta=make_delta(cost_current, cost_previous, kind="money", higher_is_better=False),
            sparkline=build_sparkline(cost_trend),
            icon="wallet",
        ),
    )

    accuracy_chart = ChartSeries(
        labels=tuple(_run_label(row) for row in accuracy_by_run),
        values=tuple(round(float(row.get("accuracy") or 0.0) * 100, 2) for row in accuracy_by_run),
        colors=(PALETTE["accent"],),
        label="Точность, %",
    )

    ordered_steps = sorted(step_duration_totals.items(), key=lambda item: -float(item[1] or 0))
    steps_chart = ChartSeries(
        labels=tuple(step_label(name) for name, _ in ordered_steps),
        values=tuple(round(float(value or 0.0), 3) for _, value in ordered_steps),
        colors=tuple(step_color(name) for name, _ in ordered_steps),
        label="Секунды",
    )

    feed = tuple(_feed_item(raw) for raw in events)
    feed_empty = (
        None
        if feed
        else EmptyState("inbox", "Событий пока нет", "Загрузить документ", "/documents")
    )

    return DashboardPage(
        title="Дашборд",
        kpis=kpis,
        accuracy_chart=accuracy_chart,
        steps_chart=steps_chart,
        feed=feed,
        feed_empty=feed_empty,
        is_empty=documents_current == 0 and not accuracy_by_run and not feed,
    )


def _run_label(row: Mapping[str, Any]) -> str:
    started = row.get("started_at")
    if isinstance(started, datetime):
        return started.strftime("%d.%m %H:%M")
    return str(row.get("label") or row.get("run_id") or "—")


_EVENT_META = {
    "document": ("file-plus-2", "Документ загружен"),
    "run": ("flask-conical", "Прогон завершён"),
    "guardrail": ("shield-alert", "Сработал guardrail"),
}


def _feed_item(raw: Mapping[str, Any]) -> FeedItem:
    kind = str(raw.get("kind", "document"))
    icon, fallback_title = _EVENT_META.get(kind, ("circle", "Событие"))
    tone = raw.get("tone")
    chip = Chip(str(raw["chip"]), tone or "muted") if raw.get("chip") else None
    return FeedItem(
        kind=kind,
        icon=icon,
        title=str(raw.get("title") or fallback_title),
        subtitle=str(raw.get("subtitle") or ""),
        time_text=filters.ru_datetime(raw.get("created_at")),
        href=raw.get("href"),
        chip=chip,
    )
