from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any

from pydantic import BaseModel

from docsift.core.config import Settings
from docsift.domain.enums import GuardrailRuleCode
from docsift.schemas.common import ExtractedField, validate_inn
from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.guardrails import GuardrailResult, GuardrailViolation

_CENT = Decimal("0.01")
_HALF_CENT = Decimal("0.005")
_ALLOWED_VAT_RATES = {Decimal(0), Decimal(10), Decimal(20)}


def _rounding_tolerance(component_count: int) -> Decimal:
    """Worst-case sum of half-cent rounding errors, rounded up to a cent."""
    raw = _HALF_CENT * max(1, component_count)
    return (raw / _CENT).to_integral_value(rounding=ROUND_CEILING) * _CENT


def _value(field: ExtractedField[Any]) -> Any:
    return field.value


def _inn_is_valid(value: str) -> bool:
    try:
        validate_inn(value)
    except ValueError:
        return False
    return True


def _walk_confidence(value: Any, path: str = "") -> list[tuple[str, float]]:
    if isinstance(value, ExtractedField):
        if value.value is None:
            return []
        return [(path or "/", value.confidence)]
    if isinstance(value, BaseModel):
        found: list[tuple[str, float]] = []
        for name in type(value).model_fields:
            child_path = f"{path}/{name}" if path else f"/{name}"
            found.extend(_walk_confidence(getattr(value, name), child_path))
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found.extend(_walk_confidence(item, f"{path}/{index}"))
        return found
    return []


def _check_total(document: ExtractedDocument) -> list[GuardrailViolation]:
    total = _value(document.total_amount)
    vat = _value(document.vat_amount)
    amounts = [_value(item.amount) for item in document.line_items]
    if total is None or vat is None or not amounts or any(amount is None for amount in amounts):
        return []

    expected_total = sum(amounts, start=Decimal(0)) + vat
    # Each item amount, document VAT and total may be rounded independently to cents.
    tolerance = _rounding_tolerance(len(amounts) + 2)
    difference = abs(total - expected_total)
    if difference <= tolerance:
        return []
    return [
        GuardrailViolation(
            rule=GuardrailRuleCode.TOTAL_MISMATCH,
            field_path="/total_amount",
            message=(
                "Итог не равен сумме позиций до НДС плюс НДС "
                f"с допустимым отклонением {tolerance}"
            ),
            expected=str(expected_total),
            actual=str(total),
        )
    ]


def _check_vat(document: ExtractedDocument) -> list[GuardrailViolation]:
    violations: list[GuardrailViolation] = []
    known_line_vat: list[Decimal] = []
    for index, item in enumerate(document.line_items):
        amount = _value(item.amount)
        rate = _value(item.vat_rate)
        vat_amount = _value(item.vat_amount)
        if rate is None:
            continue
        normalized_rate = Decimal(0) if rate == "without_vat" else Decimal(str(rate))
        path = f"/line_items/{index}/vat_rate"
        if normalized_rate not in _ALLOWED_VAT_RATES:
            violations.append(
                GuardrailViolation(
                    rule=GuardrailRuleCode.VAT_RATE_MISMATCH,
                    field_path=path,
                    message="Допустимы ставки НДС 0, 10, 20 процентов или without_vat",
                    expected="0|10|20|without_vat",
                    actual=str(rate),
                )
            )
            continue
        if amount is None or vat_amount is None:
            continue
        expected_vat = (amount * normalized_rate / Decimal(100)).quantize(_CENT)
        if abs(vat_amount - expected_vat) > _CENT:
            violations.append(
                GuardrailViolation(
                    rule=GuardrailRuleCode.VAT_RATE_MISMATCH,
                    field_path=f"/line_items/{index}/vat_amount",
                    message="НДС позиции не соответствует сумме и заявленной ставке",
                    expected=str(expected_vat),
                    actual=str(vat_amount),
                )
            )
        known_line_vat.append(vat_amount)

    document_vat = _value(document.vat_amount)
    if known_line_vat and document_vat is not None:
        expected_document_vat = sum(known_line_vat, start=Decimal(0))
        tolerance = _rounding_tolerance(len(known_line_vat) + 1)
        if abs(document_vat - expected_document_vat) > tolerance:
            violations.append(
                GuardrailViolation(
                    rule=GuardrailRuleCode.VAT_RATE_MISMATCH,
                    field_path="/vat_amount",
                    message="НДС документа не равен сумме НДС по позициям",
                    expected=str(expected_document_vat),
                    actual=str(document_vat),
                )
            )
    return violations


def _check_inn(document: ExtractedDocument) -> list[GuardrailViolation]:
    violations = []
    for party_name in ("supplier", "buyer"):
        inn = _value(getattr(document, party_name).inn)
        if inn is not None and not _inn_is_valid(inn):
            violations.append(
                GuardrailViolation(
                    rule=GuardrailRuleCode.INVALID_INN,
                    field_path=f"/{party_name}/inn",
                    message="ИНН не прошёл проверку контрольной суммы",
                    actual=inn,
                )
            )
    return violations


def _check_parties(document: ExtractedDocument) -> list[GuardrailViolation]:
    supplier_inn = _value(document.supplier.inn)
    buyer_inn = _value(document.buyer.inn)
    supplier_name = _value(document.supplier.name)
    buyer_name = _value(document.buyer.name)

    same_inn = supplier_inn is not None and supplier_inn == buyer_inn

    def normalize(value: Any) -> str:
        return " ".join(str(value).casefold().replace("ё", "е").split())

    same_name = (
        supplier_name is not None
        and buyer_name is not None
        and normalize(supplier_name) == normalize(buyer_name)
    )
    if not (same_inn or same_name):
        return []
    return [
        GuardrailViolation(
            rule=GuardrailRuleCode.SAME_PARTIES,
            field_path="/buyer",
            message="Поставщик и покупатель определены как одно лицо",
            expected="разные контрагенты",
            actual="совпадение ИНН" if same_inn else "совпадение наименования",
        )
    ]


def _check_date(
    document: ExtractedDocument,
    *,
    today: date,
    max_age_days: int,
    future_tolerance_days: int,
) -> list[GuardrailViolation]:
    document_date = _value(document.date)
    if document_date is None:
        return []
    earliest = today - timedelta(days=max_age_days)
    latest = today + timedelta(days=future_tolerance_days)
    if earliest <= document_date <= latest:
        return []
    return [
        GuardrailViolation(
            rule=GuardrailRuleCode.DOCUMENT_DATE_OUT_OF_RANGE,
            field_path="/date",
            message="Дата документа находится вне настроенного разумного диапазона",
            expected=f"{earliest.isoformat()}..{latest.isoformat()}",
            actual=document_date.isoformat(),
        )
    ]


def _check_confidence(
    document: ExtractedDocument,
    threshold: float,
) -> list[GuardrailViolation]:
    return [
        GuardrailViolation(
            rule=GuardrailRuleCode.LOW_CONFIDENCE,
            field_path=path,
            message=f"Уверенность поля ниже порога {threshold:.2f}",
            expected=threshold,
            actual=confidence,
        )
        for path, confidence in _walk_confidence(document)
        if confidence < threshold
    ]


def _mark_blocking_violations(violations: list[GuardrailViolation]) -> list[GuardrailViolation]:
    """Mark violations as blocking or non-blocking.

    Blocking violations (structural/Pydantic errors):
    - TOTAL_MISMATCH: total doesn't match line items + VAT
    - VAT_RATE_MISMATCH: VAT calculations don't match
    - SAME_PARTIES: supplier and buyer are the same

    Non-blocking violations (warnings that can be confirmed):
    - INVALID_INN: INN checksum validation (might be synthetic test data)
    - DOCUMENT_DATE_OUT_OF_RANGE: date validation (might be correct in document)
    - LOW_CONFIDENCE: confidence threshold (might be acceptable)
    """
    blocking_rules = {
        GuardrailRuleCode.TOTAL_MISMATCH,
        GuardrailRuleCode.VAT_RATE_MISMATCH,
        GuardrailRuleCode.SAME_PARTIES,
    }

    for violation in violations:
        violation.blocking = violation.rule in blocking_rules

    return violations


def evaluate_guardrails(
    document: ExtractedDocument,
    settings: Settings,
    *,
    today: date | None = None,
) -> GuardrailResult:
    current_date = today or datetime.now(UTC).date()
    violations = [
        *_check_total(document),
        *_check_vat(document),
        *_check_inn(document),
        *_check_parties(document),
        *_check_date(
            document,
            today=current_date,
            max_age_days=settings.guardrail_document_max_age_days,
            future_tolerance_days=settings.guardrail_document_future_tolerance_days,
        ),
        *_check_confidence(document, settings.guardrail_confidence_threshold),
    ]

    # Mark violations as blocking or non-blocking
    violations = _mark_blocking_violations(violations)

    # Check if we have any blocking violations or non-blocking warnings
    has_blocking = any(v.blocking for v in violations)
    has_warnings = any(not v.blocking for v in violations)

    return GuardrailResult(
        requires_review=bool(violations),
        has_warnings=has_warnings and not has_blocking,
        violations=violations
    )
