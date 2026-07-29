from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

from docsift.schemas.documents import ExtractedDocument, LineItem
from docsift.schemas.evals import (
    EvaluationMetrics,
    ExpectedDocument,
    ExpectedLineItem,
    FieldMetrics,
)

Comparator = Callable[[Any, Any], bool]

_DECIMAL_FIELDS = {
    "total_amount",
    "vat_amount",
    "line_items[].quantity",
    "line_items[].unit_price",
    "line_items[].amount",
    "line_items[].vat_rate",
    "line_items[].vat_amount",
}
_DATE_FIELDS = {"date"}
_NAME_FIELDS = {"supplier.name", "buyer.name", "line_items[].name"}
_ROOT_FIELDS = ("document_type", "number", "date", "total_amount", "vat_amount", "currency")
_PARTY_FIELDS = ("name", "inn", "kpp")
_LINE_ITEM_FIELDS = ("name", "quantity", "unit", "unit_price", "amount", "vat_rate", "vat_amount")


def _normalize_name(value: Any) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


def _name_equal(expected: Any, actual: Any, threshold: float) -> bool:
    left = _normalize_name(expected)
    right = _normalize_name(actual)
    return bool(left and right) and SequenceMatcher(None, left, right).ratio() >= threshold


def _decimal_equal(expected: Any, actual: Any) -> bool:
    try:
        return Decimal(str(expected)) == Decimal(str(actual))
    except (InvalidOperation, ValueError):
        return False


def _date_equal(expected: Any, actual: Any) -> bool:
    try:
        left = expected if isinstance(expected, date) else date.fromisoformat(str(expected))
        right = actual if isinstance(actual, date) else date.fromisoformat(str(actual))
    except ValueError:
        return False
    return left == right


def _field_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _comparator(field_path: str, name_threshold: float) -> Comparator:
    if field_path in _NAME_FIELDS:
        return lambda expected, actual: _name_equal(expected, actual, name_threshold)
    if field_path in _DECIMAL_FIELDS:
        return _decimal_equal
    if field_path in _DATE_FIELDS:
        return _date_equal
    return lambda expected, actual: expected == actual


def _record(
    metrics: EvaluationMetrics,
    field_path: str,
    expected: Any,
    actual: Any,
    name_threshold: float,
) -> None:
    counts = metrics.fields.setdefault(field_path, FieldMetrics())
    if expected is None and actual is None:
        return
    if expected is not None and actual is None:
        counts.misses += 1
    elif expected is None and actual is not None:
        counts.hallucinations += 1
    elif _comparator(field_path, name_threshold)(expected, actual):
        counts.matches += 1
    else:
        counts.mismatches += 1


def _line_item_similarity(expected: ExpectedLineItem, actual: LineItem, threshold: float) -> float:
    weighted_scores: list[tuple[float, float]] = []
    comparisons: tuple[tuple[str, float], ...] = (
        ("name", 0.50),
        ("amount", 0.20),
        ("quantity", 0.10),
        ("unit_price", 0.10),
        ("unit", 0.05),
        ("vat_rate", 0.05),
    )
    for field_name, weight in comparisons:
        expected_value = getattr(expected, field_name)
        if expected_value is None:
            continue
        actual_value = _field_value(getattr(actual, field_name))
        path = f"line_items[].{field_name}"
        score = float(
            actual_value is not None and _comparator(path, threshold)(expected_value, actual_value)
        )
        if field_name == "name" and actual_value is not None:
            score = SequenceMatcher(
                None,
                _normalize_name(expected_value),
                _normalize_name(actual_value),
            ).ratio()
        weighted_scores.append((score, weight))
    total_weight = sum(weight for _, weight in weighted_scores)
    return sum(score * weight for score, weight in weighted_scores) / total_weight if total_weight else 0


def _maximum_weight_pairs(weights: list[list[float]]) -> list[tuple[int, int, float]]:
    if not weights or not weights[0]:
        return []
    row_count = len(weights)
    column_count = len(weights[0])
    size = max(row_count, column_count)
    cost = [[1.0] * size for _ in range(size)]
    for row in range(row_count):
        for column in range(column_count):
            cost[row][column] = 1.0 - weights[row][column]

    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    matched_row = [0] * (size + 1)
    predecessor = [0] * (size + 1)
    for row in range(1, size + 1):
        matched_row[0] = row
        column = 0
        minimum = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column] = True
            current_row = matched_row[column]
            delta = float("inf")
            next_column = 0
            for candidate in range(1, size + 1):
                if used[candidate]:
                    continue
                current = cost[current_row - 1][candidate - 1] - u[current_row] - v[candidate]
                if current < minimum[candidate]:
                    minimum[candidate] = current
                    predecessor[candidate] = column
                if minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(size + 1):
                if used[candidate]:
                    u[matched_row[candidate]] += delta
                    v[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            previous = predecessor[column]
            matched_row[column] = matched_row[previous]
            column = previous
            if column == 0:
                break

    pairs: list[tuple[int, int, float]] = []
    for column in range(1, size + 1):
        row = matched_row[column]
        if 1 <= row <= row_count and column <= column_count:
            pairs.append((row - 1, column - 1, weights[row - 1][column - 1]))
    return pairs


def evaluate_document(
    expected: ExpectedDocument,
    actual: ExtractedDocument,
    *,
    name_similarity_threshold: float = 0.85,
    line_item_match_threshold: float = 0.45,
) -> EvaluationMetrics:
    metrics = EvaluationMetrics()
    for field_name in _ROOT_FIELDS:
        _record(
            metrics,
            field_name,
            getattr(expected, field_name),
            _field_value(getattr(actual, field_name)),
            name_similarity_threshold,
        )
    for party_name in ("supplier", "buyer"):
        expected_party = getattr(expected, party_name)
        actual_party = getattr(actual, party_name)
        for field_name in _PARTY_FIELDS:
            _record(
                metrics,
                f"{party_name}.{field_name}",
                getattr(expected_party, field_name),
                _field_value(getattr(actual_party, field_name)),
                name_similarity_threshold,
            )

    weights = [
        [
            _line_item_similarity(expected_item, actual_item, name_similarity_threshold)
            for actual_item in actual.line_items
        ]
        for expected_item in expected.line_items
    ]
    accepted_pairs = [
        pair for pair in _maximum_weight_pairs(weights) if pair[2] >= line_item_match_threshold
    ]
    matched_expected = {row for row, _, _ in accepted_pairs}
    matched_actual = {column for _, column, _ in accepted_pairs}
    for expected_index, actual_index, _ in accepted_pairs:
        expected_item = expected.line_items[expected_index]
        actual_item = actual.line_items[actual_index]
        for field_name in _LINE_ITEM_FIELDS:
            _record(
                metrics,
                f"line_items[].{field_name}",
                getattr(expected_item, field_name),
                _field_value(getattr(actual_item, field_name)),
                name_similarity_threshold,
            )
    for index, expected_item in enumerate(expected.line_items):
        if index not in matched_expected:
            for field_name in _LINE_ITEM_FIELDS:
                _record(
                    metrics,
                    f"line_items[].{field_name}",
                    getattr(expected_item, field_name),
                    None,
                    name_similarity_threshold,
                )
    for index, actual_item in enumerate(actual.line_items):
        if index not in matched_actual:
            for field_name in _LINE_ITEM_FIELDS:
                _record(
                    metrics,
                    f"line_items[].{field_name}",
                    None,
                    _field_value(getattr(actual_item, field_name)),
                    name_similarity_threshold,
                )
    return metrics


def merge_metrics(target: EvaluationMetrics, source: EvaluationMetrics) -> EvaluationMetrics:
    for field_path, source_counts in source.fields.items():
        target_counts = target.fields.setdefault(field_path, FieldMetrics())
        target_counts.matches += source_counts.matches
        target_counts.misses += source_counts.misses
        target_counts.hallucinations += source_counts.hallucinations
        target_counts.mismatches += source_counts.mismatches
    return target
