r"""Тесты совместимости схемы с Ollama.

Куда класть: tests/test_schema_compat.py

Запуск::

    .\.venv\Scripts\python.exe -m pytest tests/test_schema_compat.py -v --basetemp=.pytest-tmp

Сеть не нужна -- всё проверяется на структуре схемы.

Две части: вырезание ``pattern`` (иначе HTTP 500 от Ollama) и вырезание
``sources`` (иначе модель тратит половину выходных токенов на координаты).
"""

from __future__ import annotations

from typing import Any

from docsift.services.llm.schema_compat import (
    OLLAMA_UNSUPPORTED_KEYWORDS,
    drop_property,
    strip_schema_keywords,
    to_ollama_schema,
)


def _find_key(node: Any, key: str) -> bool:
    """Есть ли где-нибудь в дереве такой ключ."""
    if isinstance(node, dict):
        if key in node:
            return True
        return any(_find_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_find_key(item, key) for item in node)
    return False


def _find_property(node: Any, name: str) -> bool:
    """Есть ли где-нибудь свойство с таким именем внутри ``properties``."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict) and name in properties:
            return True
        return any(_find_property(value, name) for value in node.values())
    if isinstance(node, list):
        return any(_find_property(item, name) for item in node)
    return False


def _field_schema() -> dict[str, Any]:
    """Схема в форме ``ExtractedField``: вложенные ссылки, ``$defs``, ``required``."""
    return {
        "type": "object",
        "required": ["invoice_number", "line_items"],
        "properties": {
            "invoice_number": {"$ref": "#/$defs/ExtractedField"},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["amount", "sources"],
                    "properties": {
                        "amount": {"$ref": "#/$defs/ExtractedField"},
                        "sources": {"type": "array", "items": {"$ref": "#/$defs/SourceRef"}},
                    },
                },
            },
        },
        "$defs": {
            "ExtractedField": {
                "type": "object",
                "required": ["value", "confidence", "sources"],
                "properties": {
                    "value": {"type": ["string", "null"], "pattern": r"^\d{10}$"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "sources": {"type": "array", "items": {"$ref": "#/$defs/SourceRef"}},
                },
            },
            "SourceRef": {
                "type": "object",
                "required": ["kind", "page"],
                "properties": {
                    "kind": {"enum": ["pdf_text", "ocr"]},
                    "page": {"type": "integer"},
                },
            },
        },
    }


# --------------------------------------------------------------------------
# Часть 1: pattern
# --------------------------------------------------------------------------


def test_pattern_removed_from_nested_anyof() -> None:
    """Регулярка Decimal живёт внутри anyOf внутри $defs -- туда надо дойти."""
    schema = {
        "type": "object",
        "properties": {"total": {"$ref": "#/$defs/Money"}},
        "$defs": {
            "Money": {
                "type": "object",
                "properties": {
                    "value": {
                        "anyOf": [
                            {"type": "number", "minimum": 0.0},
                            {"type": "string", "pattern": r"^(?!^[-+.]*$)[+-]?0*$"},
                            {"type": "null"},
                        ]
                    }
                },
            }
        },
    }

    result = to_ollama_schema(schema)

    assert _find_key(schema, "pattern"), "исходная схема не должна меняться"
    assert not _find_key(result, "pattern")


def test_everything_else_survives() -> None:
    """Удаляется только pattern, остальное остаётся нетронутым."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "confidence"],
        "properties": {
            "value": {"type": "string", "pattern": r"^\d{10}$", "title": "Value"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "kind": {"enum": ["a", "b"]},
            "when": {"type": "string", "format": "date"},
        },
    }

    result = to_ollama_schema(schema)

    assert result["required"] == ["value", "confidence"]
    assert result["additionalProperties"] is False
    assert result["properties"]["value"] == {"type": "string", "title": "Value"}
    assert result["properties"]["confidence"]["maximum"] == 1
    assert result["properties"]["kind"]["enum"] == ["a", "b"]
    assert result["properties"]["when"]["format"] == "date"


def test_keyword_set_is_documented() -> None:
    assert "pattern" in OLLAMA_UNSUPPORTED_KEYWORDS
    assert strip_schema_keywords({"a": 1}, frozenset()) == {"a": 1}


# --------------------------------------------------------------------------
# Часть 2: drop_property
# --------------------------------------------------------------------------


def test_drop_property_removes_entry_and_requirement() -> None:
    """Свойство убирается и из ``properties``, и из ``required``.

    Если забыть про ``required``, Ollama потребует поле, которого в схеме
    больше нет.
    """
    result = drop_property(_field_schema(), "sources")

    extracted_field = result["$defs"]["ExtractedField"]
    assert "sources" not in extracted_field["properties"]
    assert extracted_field["required"] == ["value", "confidence"]


def test_drop_property_reaches_every_level() -> None:
    """И внутри ``$defs``, и внутри ``items`` массива позиций."""
    result = drop_property(_field_schema(), "sources")

    assert not _find_property(result, "sources")
    items = result["properties"]["line_items"]["items"]
    assert items["required"] == ["amount"]


def test_drop_property_keeps_other_fields() -> None:
    result = drop_property(_field_schema(), "sources")

    assert _find_property(result, "confidence")
    assert _find_property(result, "amount")
    assert result["required"] == ["invoice_number", "line_items"]


def test_drop_property_does_not_mutate_input() -> None:
    schema = _field_schema()

    drop_property(schema, "sources")

    assert _find_property(schema, "sources"), "исходная схема не должна меняться"


def test_drop_property_of_unknown_name_changes_nothing() -> None:
    schema = _field_schema()

    assert drop_property(schema, "нет-такого-поля") == schema


def test_drop_property_leaves_definition_in_defs() -> None:
    """Убирается свойство, а не тип: ``$defs.SourceRef`` остаётся на месте.

    Неиспользуемое определение конвертеру грамматик не мешает, но если
    когда-нибудь помешает -- сломается именно этот тест, и будет понятно где искать.
    """
    result = drop_property(_field_schema(), "sources")

    assert "SourceRef" in result["$defs"]


# --------------------------------------------------------------------------
# Часть 3: to_ollama_schema(include_sources=...)
# --------------------------------------------------------------------------


def test_sources_dropped_by_default() -> None:
    """Ради скорости цитаты восстанавливаются кодом, а не моделью."""
    result = to_ollama_schema(_field_schema())

    assert not _find_property(result, "sources")
    assert not _find_key(result, "pattern")


def test_sources_kept_when_requested() -> None:
    """Облачный профиль всё ещё может просить цитаты у модели."""
    result = to_ollama_schema(_field_schema(), include_sources=True)

    assert _find_property(result, "sources")
    assert result["$defs"]["ExtractedField"]["required"] == [
        "value",
        "confidence",
        "sources",
    ]
    assert not _find_key(result, "pattern"), "pattern вырезается в любом случае"


def test_include_sources_is_keyword_only() -> None:
    """Защита от случайного ``to_ollama_schema(schema, True)``."""
    import inspect

    parameter = inspect.signature(to_ollama_schema).parameters["include_sources"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def test_schema_is_not_mutated_by_to_ollama_schema() -> None:
    schema = _field_schema()

    to_ollama_schema(schema)

    assert _find_key(schema, "pattern")
    assert _find_property(schema, "sources")


# --------------------------------------------------------------------------
# Часть 4: настоящая схема продукта
# --------------------------------------------------------------------------


def test_extracted_document_schema_has_no_pattern() -> None:
    """Главный тест: реальная схема продукта после санитации чиста.

    Если путь к схеме у тебя другой -- поправь импорт.
    """
    from docsift.schemas.documents import ExtractedDocument

    raw = ExtractedDocument.model_json_schema()

    assert _find_key(raw, "pattern"), "если тут пусто -- тест потерял смысл, проверь импорт"
    assert not _find_key(to_ollama_schema(raw), "pattern")


def test_extracted_document_schema_has_no_sources_by_default() -> None:
    """То же самое для ``sources``: именно эта схема уходит в ``format``."""
    from docsift.schemas.documents import ExtractedDocument

    raw = ExtractedDocument.model_json_schema()

    assert _find_property(raw, "sources"), "в исходной схеме sources должен быть"
    assert not _find_property(to_ollama_schema(raw), "sources")
    assert _find_property(to_ollama_schema(raw, include_sources=True), "sources")
