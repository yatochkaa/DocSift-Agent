"""Восстановление цитат (``sources``) после ответа модели.

Куда класть: src/docsift/services/llm/sources_restore.py

Зачем
-----
Измерение на doc_01: вход 5650 токенов, выход 4430, время 691 с. Полезных
данных в ответе -- 19 полей, то есть три-четыреста токенов. Остальные
четыре тысячи -- координаты bbox в ``sources``, по четыре дробных числа на
каждое поле. Генерация примерно вчетверо медленнее чтения, поэтому
именно они съедают около восьми минут из одиннадцати.

Координаты не несут новой информации: значение уже извлечено, и найти его в
блоках текста можно поиском подстроки -- быстрее и без ошибок
переписывания.

Важное ограничение
-------------------
В ``ExtractedField.validate_evidence`` есть правило::

    if self.value is not None and not self.sources:
        raise ValueError("для извлечённого значения нужен хотя бы один source")

Поэтому подставлять цитаты надо ДО разбора в Pydantic -- прямо в сыром dict,
полученном из ``json.loads`` ответа модели::

    payload = restore_sources(json.loads(response.content), text_result)
    document = ExtractedDocument.model_validate(payload)

Соответствие координат
----------------------
В ``schemas/text_extraction.py`` у блоков ``BoundingBox`` с полями
``x0, y0, x1, y1``, а в ``schemas/common.py`` у цитат -- ``x1, y1, x2, y2``.
Перевод делает ``_bbox_payload``.

Второе расхождение: валидатор блоков разрешает вырожденный прямоугольник
(``x1 >= x0``), а валидатор цитат требует строгого ``x2 > x1``. Вырожденные
прямоугольники отбрасываются: ``bbox`` необязателен, а исключение в
валидаторе уронило бы весь разбор.

Таблицы
-------
Строки ``line_items`` часто приходят из ``ExtractedTable``, а не из текстовых
блоков, поэтому индекс строится и по ним тоже: каждая строка таблицы --
отдельная запись индекса.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

__all__ = ["restore_sources", "build_source_index"]


_SPACES = re.compile(r"[\s\u00a0\u202f]+")
_QUOTES = "\u00ab\u00bb\u201c\u201d\u201e\u2018\u2019\"'"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212"


def _normalize(text: str) -> str:
    """Привести строку к виду, удобному для сопоставления.

    Убирает пробелы (включая неразрывные и узкие), заменяет запятую на точку,
    приводит к нижнему регистру, унифицирует кавычки и тире.

    Позволяет найти «69564.00» в тексте «69 564,00», а «ООО Ромашка» --
    в «ООО «Ромашка»».
    """
    lowered = text.lower()
    for quote in _QUOTES:
        lowered = lowered.replace(quote, "")
    for dash in _DASHES:
        lowered = lowered.replace(dash, "-")
    lowered = lowered.replace(",", ".")
    return _SPACES.sub("", lowered)


def _value_variants(value: Any) -> list[str]:
    """Варианты написания значения, по которым ищем его в тексте.

    Порядок важен: сначала точное написание, потом более вольные.
    """
    if isinstance(value, bool):
        return []

    raw = str(value).strip()
    if not raw:
        return []

    variants = [raw]

    # Дата ГГГГ-ММ-ДД -> ДД.ММ.ГГГГ и Д.М.ГГГГ
    date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if date_match:
        year, month, day = date_match.groups()
        variants.append(f"{day}.{month}.{year}")
        variants.append(f"{int(day)}.{int(month)}.{year}")

    # Число с дробной частью -> без хвостовых нулей и только целая часть
    if re.fullmatch(r"-?\d+\.\d+", raw):
        trimmed = raw.rstrip("0").rstrip(".")
        if trimmed and trimmed != raw:
            variants.append(trimmed)
        variants.append(raw.split(".")[0])

    return variants


def _bbox_payload(bbox: Any) -> dict[str, float] | None:
    """Перевести bbox блока (``x0,y0,x1,y1``) в вид цитаты (``x1,y1,x2,y2``).

    Возвращает ``None``, если прямоугольник вырожден или выходит за границы
    единичного квадрата: ``BoundingBox`` в ``common.py`` требует строго
    положительных ширины и высоты.
    """
    if bbox is None:
        return None

    try:
        x1 = float(getattr(bbox, "x0"))
        y1 = float(getattr(bbox, "y0"))
        x2 = float(getattr(bbox, "x1"))
        y2 = float(getattr(bbox, "y1"))
    except (AttributeError, TypeError, ValueError):
        return None

    values = (x1, y1, x2, y2)
    if not all(0.0 <= item <= 1.0 for item in values):
        return None
    if x2 <= x1 or y2 <= y1:
        return None

    return {
        "x1": round(x1, 4),
        "y1": round(y1, 4),
        "x2": round(x2, 4),
        "y2": round(y2, 4),
    }


def build_source_index(text_result: Any) -> list[tuple[str, dict[str, Any]]]:
    """Собрать пары (нормализованный текст, готовый source_ref).

    Принимает ``TextExtractionResult``. Индекс строится один раз на документ
    и переиспользуется для всех полей.
    """
    index: list[tuple[str, dict[str, Any]]] = []

    for page in getattr(text_result, "pages", []) or []:
        page_number = int(getattr(page, "number", 1))
        kind = "ocr" if getattr(page, "used_ocr", False) else "pdf_text"

        for block in getattr(page, "blocks", []) or []:
            text = getattr(block, "text", None)
            if not text:
                continue
            ref: dict[str, Any] = {"kind": kind, "page": page_number}
            bbox = _bbox_payload(getattr(block, "bbox", None))
            if bbox is not None:
                ref["bbox"] = bbox
            ref["text"] = " ".join(str(text).split())[:500]
            index.append((_normalize(str(text)), ref))

        for table in getattr(page, "tables", []) or []:
            table_bbox = _bbox_payload(getattr(table, "bbox", None))
            for row in getattr(table, "rows", []) or []:
                joined = " ".join(str(cell) for cell in row if cell)
                if not joined.strip():
                    continue
                ref = {"kind": kind, "page": page_number}
                if table_bbox is not None:
                    ref["bbox"] = table_bbox
                ref["text"] = " ".join(joined.split())[:500]
                index.append((_normalize(joined), ref))

    return index


def _find_ref(
    value: Any,
    index: Sequence[tuple[str, dict[str, Any]]],
    fallback: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Найти блок или строку таблицы, где встречается значение."""
    for variant in _value_variants(value):
        needle = _normalize(variant)
        if len(needle) < 2:
            continue
        for haystack, ref in index:
            if needle in haystack:
                return dict(ref)
    return dict(fallback) if fallback else None


def _is_field(node: dict[str, Any]) -> bool:
    """Похож ли узел на ``ExtractedField``."""
    return "value" in node and "confidence" in node


def restore_sources(payload: Any, text_result: Any) -> Any:
    """Проставить ``sources`` во всех заполненных полях сырого ответа.

    Правила:

    * узлы с ``value is None`` не трогаем -- у них ``sources`` обязан остаться пустым;
    * узлы с уже заполненным ``sources`` не трогаем -- облачная модель всё ещё
      присылает цитаты сама;
    * если значение в тексте не найдено, ставится запасная ссылка на первую
      страницу без координат, иначе ``validate_evidence`` отвергнет поле целиком
      и верно извлечённое значение потеряется.

    Исходный объект не мутируется.
    """
    index = build_source_index(text_result)

    first_page = 1
    pages = getattr(text_result, "pages", None)
    if pages:
        first_page = int(getattr(pages[0], "number", 1))
        fallback_kind = "ocr" if getattr(pages[0], "used_ocr", False) else "pdf_text"
    else:
        fallback_kind = "pdf_text"

    fallback = {"kind": fallback_kind, "page": first_page}

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            result = {key: _walk(value) for key, value in node.items()}
            if _is_field(result) and result.get("value") is not None:
                if not result.get("sources"):
                    ref = _find_ref(result["value"], index, fallback)
                    result["sources"] = [ref] if ref else []
            return result
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(payload)
