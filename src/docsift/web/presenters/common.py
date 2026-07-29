"""Общие примитивы презентации: чипы, дельты, спарклайны, waterfall."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .. import filters
from ..tokens import ToneName, delta_arrow, delta_tone, status_label, status_tone, step_color, step_label


@dataclass(frozen=True)
class Chip:
    """Цветной чип со статусом или меткой."""

    label: str
    tone: ToneName = "muted"
    title: str | None = None


def status_chip(status: str | None) -> Chip:
    return Chip(label=status_label(status), tone=status_tone(status), title=status or None)


@dataclass(frozen=True)
class Delta:
    """Изменение к предыдущему периоду."""

    raw: float | None
    text: str
    arrow: str
    tone: ToneName
    percent: float | None = None


def make_delta(
    current: float | None,
    previous: float | None,
    *,
    kind: str = "percent",
    higher_is_better: bool = True,
    lower_is_better: bool = False,
) -> Delta:
    """Считает дельту и сразу форматирует её.

    kind: 'percent' — абсолютная разница в п.п., 'relative' — прирост в %,
          'number' — абсолютная разница, 'duration' — разница в секундах,
          'money' — разница в долларах.
    """
    if lower_is_better:
        higher_is_better = False
    if current is None or previous is None:
        return Delta(None, "нет данных", "→", "neutral")
    if kind == "relative":
        if previous == 0:
            return Delta(None, "нет данных", "→", "neutral")
        diff = (current - previous) / abs(previous)
        text = f"{'+' if diff > 0 else ''}{filters.percent(diff)}"
    else:
        diff = current - previous
        sign = "+" if diff > 0 else "−" if diff < 0 else ""
        magnitude = abs(diff)
        if kind == "percent":
            text = f"{sign}{filters.number(magnitude * 100, 1)}\u00a0п.п."
        elif kind == "duration":
            text = f"{sign}{filters.duration(magnitude)}"
        elif kind == "money":
            text = f"{sign}{filters.usd(magnitude)}"
        else:
            text = f"{sign}{filters.number(magnitude, 0)}"
        if diff == 0:
            text = "без изменений"
    ratio: float | None = None
    if previous:
        ratio = (current - previous) / abs(previous)
    return Delta(diff, text, delta_arrow(diff), delta_tone(diff, higher_is_better), ratio)


@dataclass(frozen=True)
class Sparkline:
    """Микро-график: готовый SVG-path и исходные точки."""

    points: tuple[float, ...]
    path: str
    width: int = 120
    height: int = 32


def build_sparkline(values: Sequence[float | None], width: int = 120, height: int = 32) -> Sparkline:
    """Строит SVG-path без JS. Пустой ряд или одна точка — ровная линия."""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return Sparkline((), "", width, height)
    if len(clean) == 1:
        clean = clean * 2
    lo, hi = min(clean), max(clean)
    span = hi - lo or 1.0
    step = width / (len(clean) - 1)
    pad = 3
    usable = height - pad * 2
    coords = [
        f"{i * step:.1f},{(pad + usable - (v - lo) / span * usable):.1f}" for i, v in enumerate(clean)
    ]
    return Sparkline(tuple(clean), "M" + " L".join(coords), width, height)


@dataclass(frozen=True)
class KpiCard:
    """Карточка верхней полосы дашборда."""

    key: str
    title: str
    value: str
    hint: str
    delta: Delta
    sparkline: Sparkline
    icon: str = "activity"


@dataclass(frozen=True)
class WaterfallRow:
    """Строка waterfall-диаграммы таймингов."""

    step: str
    label: str
    seconds: float
    duration_text: str
    offset_percent: float
    width_percent: float
    color: str
    share_text: str

    @property
    def width(self) -> float:
        return self.width_percent

    @property
    def offset(self) -> float:
        return self.offset_percent


def build_waterfall(
    steps: Mapping[str, float] | Iterable[tuple[str, float]],
) -> list[WaterfallRow]:
    """Раскладка шагов в полосы.

    Последовательные шаги смещаются на сумму предыдущих.
    При нулевой суммарной длительности все полосы нулевой ширины (без деления на 0).
    """
    pairs = steps.items() if hasattr(steps, "items") else steps
    items = [(name, max(0.0, float(value or 0.0))) for name, value in pairs]
    total = sum(value for _, value in items)
    rows: list[WaterfallRow] = []
    offset = 0.0
    for name, value in items:
        width = (value / total * 100) if total > 0 else 0.0
        share = (value / total) if total > 0 else 0.0
        rows.append(
            WaterfallRow(
                step=name,
                label=step_label(name),
                seconds=value,
                duration_text=filters.duration(value),
                offset_percent=round(offset, 3),
                width_percent=round(width, 3),
                color=step_color(name),
                share_text=filters.percent(share) if total > 0 else "0,0%",
            )
        )
        offset += width
    return rows


@dataclass(frozen=True)
class EmptyState:
    """Пустое состояние: иконка + одна фраза + действие."""

    icon: str
    text: str
    action_label: str | None = None
    action_href: str | None = None


@dataclass(frozen=True)
class Pagination:
    """Серверная пагинация."""

    page: int
    per_page: int
    total: int
    pages: int
    has_prev: bool
    has_next: bool
    range_text: str
    numbers: tuple[int, ...] = field(default_factory=tuple)


def build_pagination(page: int, per_page: int, total: int, window: int = 5) -> Pagination:
    per_page = max(1, per_page)
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    first = 0 if total == 0 else (page - 1) * per_page + 1
    last = min(page * per_page, total)
    start = max(1, page - window // 2)
    numbers = tuple(range(start, min(pages, start + window - 1) + 1))
    return Pagination(
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        has_prev=page > 1,
        has_next=page < pages,
        range_text=f"{filters.number(first)}\u2013{filters.number(last)} из {filters.number(total)}",
        numbers=numbers,
    )
