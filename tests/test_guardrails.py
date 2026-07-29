from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from docsift.core.config import Settings
from docsift.domain.enums import DocumentType, GuardrailRuleCode
from docsift.schemas.documents import ExtractedDocument
from docsift.services.guardrails import evaluate_guardrails


def _source() -> dict[str, Any]:
    return {"kind": "pdf_text", "page": 1, "text": "source"}


def _field(value: Any, confidence: float = 0.99) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": confidence, "sources": [_source()]}


def _line(
    *,
    amount: str = "1000.00",
    vat_rate: str = "20",
    vat_amount: str = "200.00",
) -> dict[str, Any]:
    return {
        "name": _field("Услуга"),
        "quantity": _field("1"),
        "unit": _field("шт"),
        "unit_price": _field(amount),
        "amount": _field(amount),
        "vat_rate": _field(vat_rate),
        "vat_amount": _field(vat_amount),
    }


def _document(**overrides: Any) -> ExtractedDocument:
    payload = {
        "document_type": _field(DocumentType.PAYMENT_INVOICE),
        "number": _field("42"),
        "date": _field(date(2026, 7, 25)),
        "supplier": {
            "name": _field("ООО Поставщик"),
            "inn": _field("7707083893"),
            "kpp": _field("773601001"),
        },
        "buyer": {
            "name": _field("ИП Покупатель"),
            "inn": _field("500100732259"),
            "kpp": _field(None),
        },
        "total_amount": _field("1200.00"),
        "vat_amount": _field("200.00"),
        "currency": _field("RUB"),
        "line_items": [_line()],
    }
    payload.update(overrides)
    return ExtractedDocument.model_validate(payload)


def _settings(**overrides: Any) -> Settings:
    return Settings(
        guardrail_confidence_threshold=0.8,
        guardrail_document_max_age_days=3650,
        guardrail_document_future_tolerance_days=1,
        **overrides,
    )


def _rules(document: ExtractedDocument, settings: Settings | None = None) -> list[GuardrailRuleCode]:
    result = evaluate_guardrails(document, settings or _settings(), today=date(2026, 7, 25))
    return [violation.rule for violation in result.violations]


def test_valid_document_passes_all_guardrails() -> None:
    result = evaluate_guardrails(_document(), _settings(), today=date(2026, 7, 25))
    assert result.requires_review is False
    assert result.violations == []


@pytest.mark.parametrize("total", ["1199.90", "1200.10"])
def test_total_mismatch_triggers(total: str) -> None:
    assert GuardrailRuleCode.TOTAL_MISMATCH in _rules(
        _document(total_amount=_field(total))
    )


def test_total_rounding_tolerance_does_not_trigger() -> None:
    assert GuardrailRuleCode.TOTAL_MISMATCH not in _rules(
        _document(total_amount=_field("1200.01"))
    )


def test_wrong_vat_for_rate_triggers() -> None:
    document = _document(
        total_amount=_field("1100.00"),
        vat_amount=_field("100.00"),
        line_items=[_line(vat_rate="20", vat_amount="100.00")],
    )
    assert GuardrailRuleCode.VAT_RATE_MISMATCH in _rules(document)


def test_valid_zero_ten_and_twenty_percent_vat_do_not_trigger() -> None:
    lines = [
        _line(amount="100.00", vat_rate="0", vat_amount="0.00"),
        _line(amount="100.00", vat_rate="10", vat_amount="10.00"),
        _line(amount="100.00", vat_rate="20", vat_amount="20.00"),
    ]
    document = _document(
        line_items=lines,
        vat_amount=_field("30.00"),
        total_amount=_field("330.00"),
    )
    assert GuardrailRuleCode.VAT_RATE_MISMATCH not in _rules(document)


def test_invalid_inn_checksum_triggers() -> None:
    supplier = _document().supplier.model_dump(mode="json")
    supplier["inn"] = _field("7707083894")
    assert GuardrailRuleCode.INVALID_INN in _rules(_document(supplier=supplier))


def test_valid_inn_checksum_does_not_trigger() -> None:
    assert GuardrailRuleCode.INVALID_INN not in _rules(_document())


def test_same_supplier_and_buyer_triggers() -> None:
    supplier = _document().supplier.model_dump(mode="json")
    assert GuardrailRuleCode.SAME_PARTIES in _rules(_document(buyer=supplier))


def test_different_supplier_and_buyer_do_not_trigger() -> None:
    assert GuardrailRuleCode.SAME_PARTIES not in _rules(_document())


@pytest.mark.parametrize("offset_days", [-3651, 2])
def test_document_date_out_of_range_triggers(offset_days: int) -> None:
    value = date(2026, 7, 25) + timedelta(days=offset_days)
    assert GuardrailRuleCode.DOCUMENT_DATE_OUT_OF_RANGE in _rules(
        _document(date=_field(value))
    )


def test_document_date_inside_range_does_not_trigger() -> None:
    assert GuardrailRuleCode.DOCUMENT_DATE_OUT_OF_RANGE not in _rules(_document())


def test_low_confidence_triggers_for_exact_field_path() -> None:
    result = evaluate_guardrails(
        _document(number=_field("42", confidence=0.79)),
        _settings(),
        today=date(2026, 7, 25),
    )
    paths = {
        violation.field_path
        for violation in result.violations
        if violation.rule is GuardrailRuleCode.LOW_CONFIDENCE
    }
    assert paths == {"/number"}


def test_confidence_at_threshold_does_not_trigger() -> None:
    result = evaluate_guardrails(
        _document(number=_field("42", confidence=0.80)),
        _settings(),
        today=date(2026, 7, 25),
    )
    number_violations = [
        violation
        for violation in result.violations
        if violation.rule is GuardrailRuleCode.LOW_CONFIDENCE
        and violation.field_path == "/number"
    ]
    assert number_violations == []


def test_invalid_inn_is_confirmable_warning() -> None:
    supplier = _document().supplier.model_dump(mode="json")
    supplier["inn"] = _field("7707083894")
    result = evaluate_guardrails(
        _document(supplier=supplier),
        _settings(),
        today=date(2026, 7, 25),
    )
    violations = [
        violation
        for violation in result.violations
        if violation.rule is GuardrailRuleCode.INVALID_INN
    ]
    assert len(violations) == 1
    assert violations[0].blocking is False
    assert result.has_warnings is True


def test_same_parties_is_blocking_violation() -> None:
    supplier = _document().supplier.model_dump(mode="json")
    result = evaluate_guardrails(
        _document(buyer=supplier),
        _settings(),
        today=date(2026, 7, 25),
    )
    violations = [
        violation
        for violation in result.violations
        if violation.rule is GuardrailRuleCode.SAME_PARTIES
    ]
    assert len(violations) == 1
    assert violations[0].blocking is True
