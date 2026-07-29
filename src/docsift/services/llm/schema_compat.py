"""Совместимость JSON Schema с конвертером грамматик Ollama.

Куда класть: src/docsift/services/llm/schema_compat.py (замена прежней версии)

Часть 1. Ключ ``pattern``
-------------------------
Ollama переводит JSON Schema из поля ``format`` в GBNF-грамматику. Ключ
``pattern`` он переваривает плохо:

* регулярки Pydantic для ``Decimal`` содержат lookahead и lookbehind
  (``(?!...)``, ``(?=...)``), которые в GBNF не выражаются вообще ->
  HTTP 500 ``invalid JSON schema in format``;
* даже простые регулярки вроде ``^\\d{10}(\\d{2})?$`` (ИНН) и ``^\\d{9}$``
  (КПП) роняют процесс модели -> HTTP 500 ``model runner has unexpectedly
  stopped``.

Убираем ``pattern`` перед отправкой. На корректность это не влияет: схема в
``format`` только направляет генерацию, а фактическая валидация ответа
происходит позже -- в Pydantic (формат) и в guardrails (смысл, контрольная
сумма ИНН, даты).

Часть 2. Поле ``sources``
-------------------------
Измерение на doc_01: вход 5650 токенов, выход 4430. Выход почти сравнялся со
входом, хотя полезных данных в нём в разы меньше. Причина -- ``sources``:
для каждого из ~40 полей модель переписывает ``source_ref`` с координатами
bbox из четырёх дробных чисел. Это самая дорогая часть ответа, потому что
генерация примерно вчетверо медленнее чтения промта.

Координаты не несут новой информации: значение уже извлечено, и найти его в
блоках текста можно поиском подстроки -- быстрее и без ошибок переписывания.
Поэтому ``sources`` вырезается из схемы, уходящей в ``format``, а цитаты
восстанавливаются после ответа (см. restore_sources в service.py).

ВАЖНО: чтобы Pydantic принял ответ без ``sources``, поле в ExtractedField
должно иметь значение по умолчанию, например::

    sources: list[SourceRef] = Field(default_factory=list)

Без этого разбор упадёт с ошибкой обязательного поля.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "OLLAMA_UNSUPPORTED_KEYWORDS",
    "strip_schema_keywords",
    "drop_property",
    "to_ollama_schema",
]


#: Ключи JSON Schema, которые ломают конвертер грамматик Ollama.
OLLAMA_UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset({"pattern"})


def strip_schema_keywords(node: Any, keywords: frozenset[str]) -> Any:
    """Рекурсивно удалить ключи на всех уровнях схемы.

    Заходит внутрь ``$defs``, ``properties``, ``items``, ``anyOf`` и любых других
    вложенных структур. Исходный объект не мутируется.
    """
    if isinstance(node, dict):
        return {
            key: strip_schema_keywords(value, keywords)
            for key, value in node.items()
            if key not in keywords
        }
    if isinstance(node, list):
        return [strip_schema_keywords(item, keywords) for item in node]
    return node


def drop_property(node: Any, name: str) -> Any:
    """Рекурсивно удалить свойство ``name`` из всех объектов схемы.

    Удаляет запись из ``properties`` и одновременно вычищает имя из списка
    ``required``, иначе Ollama потребует поле, которого в схеме больше нет.
    Исходный объект не мутируется.
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                result[key] = {
                    prop_name: drop_property(prop_value, name)
                    for prop_name, prop_value in value.items()
                    if prop_name != name
                }
            elif key == "required" and isinstance(value, list):
                result[key] = [item for item in value if item != name]
            else:
                result[key] = drop_property(value, name)
        return result
    if isinstance(node, list):
        return [drop_property(item, name) for item in node]
    return node


def to_ollama_schema(
    schema: dict[str, Any],
    *,
    include_sources: bool = False,
) -> dict[str, Any]:
    """Привести JSON Schema к виду, который принимает Ollama в поле ``format``.

    :param include_sources: оставить ли поле ``sources`` в схеме. По умолчанию
        выключено ради скорости: цитаты восстанавливаются программно после
        получения ответа.
    """
    result = strip_schema_keywords(schema, OLLAMA_UNSUPPORTED_KEYWORDS)
    if not include_sources:
        result = drop_property(result, "sources")
    return result
