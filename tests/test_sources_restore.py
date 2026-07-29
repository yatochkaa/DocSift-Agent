r"""Тесты восстановления цитат (``sources``) вне модели.

Куда класть: tests/test_sources_restore.py

Запуск::

    .\.venv\Scripts\python.exe -m pytest tests/test_sources_restore.py -v --basetemp=.pytest-tmp

Ни сети, ни Ollama, ни PDF не нужно.

Почему здесь подставные классы, а не настоящие схемы
---------------------------------------------------------
``sources_restore`` читает результат извлечения текста только через ``getattr``
с умолчаниями и ничего не валидирует. Лёгкие датаклассы дают тот же
интерфейс, но тесты не ломаются при правке Pydantic-моделей и не требуют
выдумывать валидные значения для всех обязательных полей.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from docsift.services.llm.sources_restore import (
    _bbox_payload,
    _normalize,
    _value_variants,
    build_source_index,
    restore_sources,
)

# --------------------------------------------------------------------------
# Подставные объекты извлечения текста
# --------------------------------------------------------------------------


@dataclass
class FakeBBox:
    """Координаты в соглашении text_extraction: x0, y0, x1, y1."""

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class FakeBlock:
    text: str
    bbox: FakeBBox | None = None


@dataclass
class FakeTable:
    rows: list[list[str]] = field(default_factory=list)
    bbox: FakeBBox | None = None


@dataclass
class FakePage:
    number: int = 1
    used_ocr: bool = False
    blocks: list[FakeBlock] = field(default_factory=list)
    tables: list[FakeTable] = field(default_factory=list)


@dataclass
class FakeTextResult:
    pages: list[FakePage] = field(default_factory=list)


def make_field(value: Any, confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    """Узел, который ``_is_field`` признаёт за ``ExtractedField``."""
    node: dict[str, Any] = {"value": value, "confidence": confidence}
    node.update(extra)
    return node


# --------------------------------------------------------------------------
# _normalize
# --------------------------------------------------------------------------


def test_normalize_strips_all_kinds_of_spaces() -> None:
    """Обычный, неразрывный и узкий пробелы исчезают одинаково.

    В PDF счёта разряды часто разделены именно U+00A0 или U+202F.
    """
    assert _normalize("69 564,00") == "69564.00"
    assert _normalize("69\u00a0564,00") == "69564.00"
    assert _normalize("69\u202f564,00") == "69564.00"
    assert _normalize("69\t564\n,00") == "69564.00"


def test_normalize_drops_quotes_and_unifies_dashes() -> None:
    assert _normalize("ООО «Ромашка»") == _normalize("ООО Ромашка")
    assert _normalize('ООО "Ромашка"') == "оооромашка"
    assert _normalize("счёт — 143") == _normalize("счёт - 143")
    assert _normalize("МИНУС−143") == "минус-143"


def test_normalize_is_case_insensitive() -> None:
    assert _normalize("ИНН 7714236789") == _normalize("инн 7714236789")


# --------------------------------------------------------------------------
# _value_variants
# --------------------------------------------------------------------------


def test_value_variants_ignores_booleans() -> None:
    """Для ``True`` поиск по тексту бессмыслен: bool проверяется до int."""
    assert _value_variants(True) == []
    assert _value_variants(False) == []


def test_value_variants_ignores_empty() -> None:
    assert _value_variants("") == []
    assert _value_variants("   ") == []


def test_value_variants_expands_iso_date() -> None:
    """В документе дата написана по-русски, в ответе модели -- по ISO."""
    variants = _value_variants("2025-03-12")

    assert variants[0] == "2025-03-12", "точное написание ищется первым"
    assert "12.03.2025" in variants
    assert "12.3.2025" in variants


def test_value_variants_trims_trailing_zeros() -> None:
    """69564.0 из JSON должно находиться в «69 564,00» из PDF."""
    assert _value_variants("69564.00")[0] == "69564.00"
    assert "69564" in _value_variants("69564.00")
    assert "1800.5" in _value_variants("1800.50")
    assert "1800" in _value_variants("1800.50")
    assert "69564" in _value_variants(69564.0)


def test_value_variants_keeps_plain_strings_as_is() -> None:
    assert _value_variants("ООО «Ромашка»") == ["ООО «Ромашка»"]
    assert _value_variants("  143  ") == ["143"]


# --------------------------------------------------------------------------
# _bbox_payload
# --------------------------------------------------------------------------


def test_bbox_translates_coordinate_names() -> None:
    """x0,y0,x1,y1 из блока -> x1,y1,x2,y2 из цитаты."""
    payload = _bbox_payload(FakeBBox(0.1, 0.2, 0.3, 0.4))

    assert payload == {"x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.4}


def test_bbox_rounds_to_four_digits() -> None:
    payload = _bbox_payload(FakeBBox(0.123456, 0.2, 0.987654, 0.4))

    assert payload == {"x1": 0.1235, "y1": 0.2, "x2": 0.9877, "y2": 0.4}


def test_bbox_rejects_degenerate_rectangle() -> None:
    """Главная ловушка: common.BoundingBox требует строгого x2 > x1.

    Валидатор блоков нулевую ширину разрешает, и если такой прямоугольник
    утечёт в цитату, Pydantic уронит разбор всего документа.
    """
    assert _bbox_payload(FakeBBox(0.3, 0.2, 0.3, 0.4)) is None
    assert _bbox_payload(FakeBBox(0.1, 0.4, 0.3, 0.4)) is None
    assert _bbox_payload(FakeBBox(0.5, 0.2, 0.1, 0.4)) is None


def test_bbox_rejects_coordinates_outside_unit_square() -> None:
    """Координаты нормализованы; точки в пунктах -- признак ошибки."""
    assert _bbox_payload(FakeBBox(0.1, 0.2, 1.2, 0.4)) is None
    assert _bbox_payload(FakeBBox(-0.1, 0.2, 0.3, 0.4)) is None
    assert _bbox_payload(FakeBBox(0.0, 0.0, 1.0, 1.0)) is not None


def test_bbox_survives_missing_or_broken_input() -> None:
    assert _bbox_payload(None) is None
    assert _bbox_payload(object()) is None
    assert _bbox_payload(FakeBBox("слева", 0.2, 0.3, 0.4)) is None


# --------------------------------------------------------------------------
# build_source_index
# --------------------------------------------------------------------------


def test_index_collects_blocks_with_page_and_kind() -> None:
    text_result = FakeTextResult(
        pages=[
            FakePage(
                number=2,
                blocks=[FakeBlock("Счёт  № 143\nот 12.03.2025", FakeBBox(0.1, 0.1, 0.5, 0.2))],
            )
        ]
    )

    index = build_source_index(text_result)

    assert len(index) == 1
    haystack, ref = index[0]
    assert "12.03.2025" in haystack
    assert ref["page"] == 2
    assert ref["kind"] == "pdf_text"
    assert ref["bbox"] == {"x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.2}
    assert ref["text"] == "Счёт № 143 от 12.03.2025", "пробелы схлопываются"


def test_index_marks_ocr_pages() -> None:
    text_result = FakeTextResult(
        pages=[FakePage(used_ocr=True, blocks=[FakeBlock("скан счёта")])]
    )

    _, ref = build_source_index(text_result)[0]

    assert ref["kind"] == "ocr"


def test_index_omits_bbox_when_block_has_none() -> None:
    text_result = FakeTextResult(pages=[FakePage(blocks=[FakeBlock("без координат")])])

    _, ref = build_source_index(text_result)[0]

    assert "bbox" not in ref, "bbox необязателен, пустого ключа быть не должно"


def test_index_skips_empty_blocks_and_rows() -> None:
    text_result = FakeTextResult(
        pages=[
            FakePage(
                blocks=[FakeBlock(""), FakeBlock("есть текст")],
                tables=[FakeTable(rows=[["", ""], [" ", None], ["Колодки", "2"]])],
            )
        ]
    )

    index = build_source_index(text_result)

    assert len(index) == 2


def test_index_indexes_each_table_row_separately() -> None:
    """Позиции счёта приходят из таблицы, а не из текстовых блоков."""
    table = FakeTable(
        rows=[
            ["1", "Тормозные колодки", "2", "18 696,00"],
            ["2", "Амортизаторы", "4", "19 728,00"],
        ],
        bbox=FakeBBox(0.05, 0.4, 0.95, 0.6),
    )
    text_result = FakeTextResult(pages=[FakePage(tables=[table])])

    index = build_source_index(text_result)

    assert len(index) == 2
    assert all(ref["bbox"] == {"x1": 0.05, "y1": 0.4, "x2": 0.95, "y2": 0.6} for _, ref in index)
    assert "18696.00" in index[0][0]
    assert index[0][1]["text"] == "1 Тормозные колодки 2 18 696,00"


def test_index_truncates_quote_text() -> None:
    text_result = FakeTextResult(pages=[FakePage(blocks=[FakeBlock("а" * 600)])])

    _, ref = build_source_index(text_result)[0]

    assert len(ref["text"]) == 500


def test_index_of_empty_result_is_empty() -> None:
    assert build_source_index(FakeTextResult()) == []


# --------------------------------------------------------------------------
# restore_sources
# --------------------------------------------------------------------------


def _single_page_result() -> FakeTextResult:
    return FakeTextResult(
        pages=[
            FakePage(
                number=1,
                blocks=[
                    FakeBlock("Счёт № 143 от 12.03.2025", FakeBBox(0.1, 0.05, 0.6, 0.1)),
                    FakeBlock(
                        "Поставщик: ООО «Ромашка», ИНН 7714236789",
                        FakeBBox(0.1, 0.15, 0.9, 0.2),
                    ),
                    FakeBlock("Всего к оплате: 69\u00a0564,00", FakeBBox(0.5, 0.8, 0.9, 0.85)),
                ],
            )
        ]
    )


def test_restore_fills_sources_from_block() -> None:
    payload = {"invoice_number": make_field("143")}

    result = restore_sources(payload, _single_page_result())

    sources = result["invoice_number"]["sources"]
    assert len(sources) == 1
    assert sources[0]["page"] == 1
    assert sources[0]["kind"] == "pdf_text"
    assert sources[0]["bbox"] == {"x1": 0.1, "y1": 0.05, "x2": 0.6, "y2": 0.1}
    assert "143" in sources[0]["text"]


def test_restore_matches_amount_written_with_nbsp_and_comma() -> None:
    """Модель отдаёт 69564.0, в PDF написано «69\u00a0564,00»."""
    payload = {"total_amount": make_field(69564.0)}

    result = restore_sources(payload, _single_page_result())

    assert result["total_amount"]["sources"][0]["bbox"] == {
        "x1": 0.5,
        "y1": 0.8,
        "x2": 0.9,
        "y2": 0.85,
    }


def test_restore_matches_iso_date_against_russian_format() -> None:
    payload = {"invoice_date": make_field("2025-03-12")}

    result = restore_sources(payload, _single_page_result())

    assert "12.03.2025" in result["invoice_date"]["sources"][0]["text"]


def test_restore_matches_party_name_with_guillemets() -> None:
    payload = {"supplier": {"name": make_field("ООО Ромашка")}}

    result = restore_sources(payload, _single_page_result())

    assert "Ромашка" in result["supplier"]["name"]["sources"][0]["text"]


def test_restore_walks_into_line_items_and_uses_table_rows() -> None:
    table = FakeTable(
        rows=[
            ["1", "Тормозные колодки", "2", "18 696,00"],
            ["2", "Амортизаторы", "4", "19 728,00"],
        ],
        bbox=FakeBBox(0.05, 0.4, 0.95, 0.6),
    )
    text_result = FakeTextResult(pages=[FakePage(tables=[table])])
    payload = {
        "line_items": [
            {"name": make_field("Тормозные колодки"), "amount": make_field(18696.0)},
            {"name": make_field("Амортизаторы"), "amount": make_field(19728.0)},
        ]
    }

    result = restore_sources(payload, text_result)

    for item in result["line_items"]:
        for node in item.values():
            assert len(node["sources"]) == 1
    assert "Тормозные колодки" in result["line_items"][0]["amount"]["sources"][0]["text"]
    assert "Амортизаторы" in result["line_items"][1]["amount"]["sources"][0]["text"]


def test_restore_leaves_null_values_without_sources() -> None:
    """У пустого поля ``sources`` обязан остаться пустым."""
    payload = {"kpp": make_field(None), "contract": make_field(None, sources=[])}

    result = restore_sources(payload, _single_page_result())

    assert "sources" not in result["kpp"]
    assert result["contract"]["sources"] == []


def test_restore_keeps_sources_supplied_by_the_model() -> None:
    """Облачная ветка всё ещё присылает цитаты сама -- не трогаем."""
    own = [{"kind": "pdf_text", "page": 7, "text": "от модели"}]
    payload = {"invoice_number": make_field("143", sources=own)}

    result = restore_sources(payload, _single_page_result())

    assert result["invoice_number"]["sources"] == own


def test_restore_falls_back_when_value_is_absent_from_text() -> None:
    """Без запасной ссылки validate_evidence убьёт верное значение целиком."""
    payload = {"currency": make_field("USD")}

    result = restore_sources(payload, _single_page_result())

    assert result["currency"]["sources"] == [{"kind": "pdf_text", "page": 1}]


def test_restore_falls_back_for_too_short_value() -> None:
    """Один символ совпадёт где угодно, поэтому поиск его пропускает."""
    payload = {"line_count": make_field(4)}

    result = restore_sources(payload, _single_page_result())

    assert result["line_count"]["sources"] == [{"kind": "pdf_text", "page": 1}]


def test_restore_falls_back_for_boolean_value() -> None:
    payload = {"vat_included": make_field(True)}

    result = restore_sources(payload, _single_page_result())

    assert result["vat_included"]["sources"] == [{"kind": "pdf_text", "page": 1}]


def test_restore_fallback_follows_the_first_page() -> None:
    text_result = FakeTextResult(
        pages=[FakePage(number=3, used_ocr=True, blocks=[FakeBlock("скан")])]
    )
    payload = {"currency": make_field("USD")}

    result = restore_sources(payload, text_result)

    assert result["currency"]["sources"] == [{"kind": "ocr", "page": 3}]


def test_restore_survives_result_without_pages() -> None:
    payload = {"invoice_number": make_field("143")}

    result = restore_sources(payload, FakeTextResult())

    assert result["invoice_number"]["sources"] == [{"kind": "pdf_text", "page": 1}]


def test_restore_ignores_nodes_that_are_not_fields() -> None:
    """Узел без ``confidence`` -- не ``ExtractedField``, ему цитаты не нужны."""
    payload = {"meta": {"value": "143"}, "doc_type": "invoice"}

    result = restore_sources(payload, _single_page_result())

    assert result == payload


def test_restore_does_not_mutate_the_input() -> None:
    payload = {"invoice_number": make_field("143")}
    untouched = copy.deepcopy(payload)

    restore_sources(payload, _single_page_result())

    assert payload == untouched


def test_restore_on_doc_01_shaped_payload() -> None:
    """Сборка целиком на форме эталонного счёта № 143.

    Заголовок и итоги -- из текстовых блоков, четыре позиции -- из таблицы.
    Ни одно заполненное поле не должно остаться без цитаты.
    """
    text_result = FakeTextResult(
        pages=[
            FakePage(
                number=1,
                blocks=[
                    FakeBlock("Счёт № 143 от 12.03.2025", FakeBBox(0.1, 0.05, 0.6, 0.1)),
                    FakeBlock(
                        "Поставщик: ООО «Ромашка» ИНН 7714236789 КПП 771401001",
                        FakeBBox(0.1, 0.15, 0.9, 0.2),
                    ),
                    FakeBlock(
                        "Покупатель: ООО «ТехноСервис Плюс» ИНН 5029154872",
                        FakeBBox(0.1, 0.22, 0.9, 0.27),
                    ),
                    FakeBlock(
                        "Без НДС 57\u00a0970,00 НДС 20 % 11\u00a0594,00 Всего 69\u00a0564,00",
                        FakeBBox(0.5, 0.8, 0.95, 0.86),
                    ),
                ],
                tables=[
                    FakeTable(
                        rows=[
                            ["1", "Тормозные колодки", "2", "9 348,00", "18 696,00"],
                            ["2", "Амортизаторы", "4", "4 932,00", "19 728,00"],
                            ["3", "Фильтры масляные", "12", "2 445,00", "29 340,00"],
                            ["4", "Доставка", "1", "1 800,00", "1 800,00"],
                        ],
                        bbox=FakeBBox(0.05, 0.35, 0.95, 0.7),
                    )
                ],
            )
        ]
    )
    payload = {
        "invoice_number": make_field("143"),
        "invoice_date": make_field("2025-03-12"),
        "supplier": {
            "name": make_field("ООО Ромашка"),
            "inn": make_field("7714236789"),
            "kpp": make_field("771401001"),
        },
        "buyer": {
            "name": make_field("ООО ТехноСервис Плюс"),
            "inn": make_field("5029154872"),
            "kpp": make_field(None),
        },
        "line_items": [
            {"name": make_field("Тормозные колодки"), "amount": make_field(18696.0)},
            {"name": make_field("Амортизаторы"), "amount": make_field(19728.0)},
            {"name": make_field("Фильтры масляные"), "amount": make_field(29340.0)},
            {"name": make_field("Доставка"), "amount": make_field(1800.0)},
        ],
        "amount_without_vat": make_field(57970.0),
        "vat_amount": make_field(11594.0),
        "total_amount": make_field(69564.0),
    }

    result = restore_sources(payload, text_result)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "value" in node and "confidence" in node:
                if node["value"] is None:
                    assert not node.get("sources")
                else:
                    assert len(node["sources"]) == 1, node
                return
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)

    assert "Счёт" in result["invoice_number"]["sources"][0]["text"]
    assert "Фильтры" in result["line_items"][2]["amount"]["sources"][0]["text"]
    assert result["total_amount"]["sources"][0]["bbox"]["y1"] == 0.8
    assert result["line_items"][3]["amount"]["sources"][0]["bbox"]["y1"] == 0.35
