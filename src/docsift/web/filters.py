"""Jinja-фильтры форматирования. Русская локаль, без сторонних зависимостей.

Разделитель разрядов — неразрывный пробел (U+00A0), дробная часть — запятая,
даты — 12.03.2025.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

NBSP = "\u00a0"


def _split_thousands(text: str) -> str:
    sign = "-" if text.startswith("-") else ""
    digits = text.lstrip("-")
    groups: list[str] = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return sign + NBSP.join(groups)


def number(value: Any, digits: int = 0) -> str:
    """1234567.5 -> '1 234 567,5' (неразрывные пробелы)."""
    if value is None or value == "":
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    formatted = f"{num:.{digits}f}"
    whole, _, frac = formatted.partition(".")
    out = _split_thousands(whole)
    return f"{out},{frac}" if frac else out


def money(value: Any, currency: str = "₽", digits: int = 2) -> str:
    """Денежная сумма с неразрывным пробелом перед знаком валюты."""
    if value is None or value == "":
        return "—"
    return f"{number(value, digits)}{NBSP}{currency}"


def usd(value: Any, digits: int = 4) -> str:
    """Стоимость прогонов LLM обычно в долларах и с мелкой дробной частью."""
    if value is None:
        return "—"
    digits = 2 if abs(float(value)) >= 1 else digits
    return f"${number(value, digits)}"


def percent(value: Any, digits: int = 1) -> str:
    """0.9123 -> '91,2%'. Значения > 1 считаются уже процентами."""
    if value is None or value == "":
        return "—"
    num = float(value)
    if -1.0 <= num <= 1.0:
        num *= 100
    return f"{number(num, digits)}{NBSP}%"


def ru_date(value: Any) -> str:
    """datetime/date/ISO-строка -> '12.03.2025'."""
    dt = _to_datetime(value)
    return dt.strftime("%d.%m.%Y") if dt else "—"


def ru_datetime(value: Any) -> str:
    """'12.03.2025, 14:05'."""
    dt = _to_datetime(value)
    return dt.strftime("%d.%m.%Y, %H:%M") if dt else "—"


def duration(value: Any) -> str:
    """Длительность в секундах -> '820 мс' / '4,2 с' / '3 мин 12 с'."""
    if value is None:
        return "—"
    seconds = float(value)
    if seconds == 0:
        return f"0{NBSP}\u0441"
    if seconds < 1:
        return f"{number(seconds * 1000, 0)}{NBSP}мс"
    if seconds < 60:
        return f"{number(seconds, 1)}{NBSP}с"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}{NBSP}мин {rest}{NBSP}с"


def tokens(value: Any) -> str:
    if value is None:
        return "—"
    return number(value, 0)


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


FILTERS = {
    "number": number,
    "money": money,
    "usd": usd,
    "percent": percent,
    "ru_date": ru_date,
    "ru_datetime": ru_datetime,
    "duration": duration,
    "tokens": tokens,
}


def register(env) -> None:
    """Регистрирует все фильтры в окружении Jinja."""
    env.filters.update(FILTERS)
