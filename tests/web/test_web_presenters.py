"""Чистые проверки презентеров и форматтеров — без HTTP и без шаблонов."""

from __future__ import annotations

from datetime import datetime, timezone

from docsift.web import filters
from docsift.web.presenters.common import build_waterfall, make_delta
from docsift.web.presenters.compare import build_compare
from docsift.web.tokens import confidence_tone, heat_cell, status_tone

from .conftest import RUN, RUN_B

NBSP = "\u00a0"


def test_money_uses_nbsp_thousand_separator():
    assert filters.money(1234567.5, "₽") == f"1{NBSP}234{NBSP}567,50{NBSP}₽"


def test_number_formats_integers():
    assert filters.number(1000) == f"1{NBSP}000"
    assert filters.number(None) == "—"


def test_ru_date_format():
    assert filters.ru_date(datetime(2025, 3, 12, tzinfo=timezone.utc)) == "12.03.2025"


def test_percent_and_duration():
    assert filters.percent(0.912) == "91,2 %".replace("\u00a0", NBSP)
    assert filters.duration(0) == f"0{NBSP}с"
    assert filters.duration(184.2).endswith("м") or "с" in filters.duration(184.2)


def test_confidence_thresholds():
    assert confidence_tone(0.95) == "success"
    assert confidence_tone(0.9) == "success"
    assert confidence_tone(0.8) == "warning"
    assert confidence_tone(0.7) == "warning"
    assert confidence_tone(0.69) == "danger"


def test_status_tone_has_fallback():
    assert status_tone("completed") == "success"
    assert status_tone("failed") == "danger"
    assert status_tone("неизвестный") == "neutral"


def test_heat_cell_produces_style_for_value_and_dash_for_none():
    cell = heat_cell(0.95)
    assert "background" in cell.style
    assert heat_cell(None).text == "—"


def test_waterfall_with_zero_total_does_not_divide_by_zero():
    rows = build_waterfall({"text_extraction": 0.0, "llm_extraction": 0.0})
    assert rows
    assert all(row.width == 0.0 for row in rows)
    assert all(row.offset == 0.0 for row in rows)


def test_waterfall_widths_sum_to_hundred():
    rows = build_waterfall({"text_extraction": 25.0, "llm_extraction": 75.0})
    assert round(sum(row.width for row in rows), 3) == 100.0
    assert rows[0].offset == 0.0
    assert round(rows[1].offset, 3) == 25.0


def test_make_delta_signs_and_tones():
    up = make_delta(120, 100)
    assert up.tone == "success"
    assert up.arrow == "↑"

    down = make_delta(80, 100)
    assert down.tone == "danger"
    assert down.arrow == "↓"

    flat = make_delta(100, 100)
    assert flat.tone == "neutral"

    # При нулевой базе процент не считаем — нет деления на ноль.
    assert make_delta(5, 0).percent is None


def test_make_delta_lower_is_better():
    faster = make_delta(80, 100, lower_is_better=True)
    assert faster.tone == "success"


def test_compare_detects_improved_and_degraded_fields():
    page = build_compare(RUN, RUN_B)
    assert page.summary_improved
    assert page.summary_degraded
    improved = " ".join(page.summary_improved)
    degraded = " ".join(page.summary_degraded)
    assert "total_amount" in improved
    assert "counterparty" in degraded


def test_compare_performance_rows_cover_time_and_cost():
    page = build_compare(RUN, RUN_B)
    labels = [row.label for row in page.performance]
    assert "Длительность" in labels
    assert "Стоимость" in labels


# --- bbox: объектная и списочная форма ---------------------------------------


def test_source_box_accepts_dict_bbox():
    """Реальная форма из схемы BoundingBox: {x1, y1, x2, y2}."""
    from docsift.web.presenters.document_detail import _source_box

    box = _source_box({"page": 2, "bbox": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.4}})
    assert box.page == 2
    assert "left:10.000%" in box.style
    assert "top:20.000%" in box.style
    assert "width:40.000%" in box.style
    assert "height:20.000%" in box.style


def test_source_box_accepts_list_bbox():
    from docsift.web.presenters.document_detail import _source_box

    box = _source_box({"page": 1, "bbox": [0.1, 0.2, 0.5, 0.4]})
    assert "left:10.000%" in box.style
    assert "width:40.000%" in box.style


def test_source_box_survives_broken_bbox():
    from docsift.web.presenters.document_detail import _source_box

    box = _source_box({"page": 1, "bbox": {"x1": None, "y1": "нет"}})
    assert "width:0.000%" in box.style
