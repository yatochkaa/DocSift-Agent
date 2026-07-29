from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from docsift.schemas.common import (
    CurrencyCode,
    ExtractedField,
    SourceKind,
    SourceRef,
    normalize_currency,
)
from docsift.schemas.documents import DocumentType, ExtractedDocument, Party


@pytest.fixture
def source() -> SourceRef:
    return SourceRef(kind=SourceKind.PDF_TEXT, page=1, text="фрагмент документа")


def field(value, source: SourceRef, confidence: float = 0.99) -> dict:
    """Имитирует входной JSON, чтобы Pydantic создал нужную специализацию generic-модели."""
    return {
        "value": value,
        "confidence": confidence,
        "sources": [source.model_dump()],
    }


def party(inn: str, source: SourceRef) -> Party:
    return Party(
        name=field("ООО Контрагент", source),
        inn=field(inn, source),
        kpp=field("773601001", source),
    )


def document(source: SourceRef, **overrides) -> dict:
    data = {
        "document_type": field(DocumentType.PAYMENT_INVOICE, source),
        "number": field("42", source),
        "date": field(datetime.now(UTC).date(), source),
        "supplier": party("7707083893", source),
        "buyer": party("500100732259", source),
        "total_amount": field(Decimal("1200.00"), source),
        "vat_amount": field(Decimal("200.00"), source),
        "currency": field("RUB", source),
        "line_items": [],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("inn", ["7707083893", "500100732259"])
def test_accepts_valid_inn(inn: str, source: SourceRef) -> None:
    assert party(inn, source).inn.value == inn


@pytest.mark.parametrize("inn", ["123", "abcdefghij"])
def test_rejects_malformed_inn(inn: str, source: SourceRef) -> None:
    with pytest.raises(ValidationError):
        party(inn, source)


def test_allows_invalid_inn_checksum_for_guardrail_review(source: SourceRef) -> None:
    assert party("7707083894", source).inn.value == "7707083894"


def test_allows_future_document_date_for_guardrail_review(source: SourceRef) -> None:
    future_date = datetime.now(UTC).date() + timedelta(days=2)
    future = field(future_date, source)
    assert ExtractedDocument(**document(source, date=future)).date.value == future_date


def test_rejects_negative_money(source: SourceRef) -> None:
    negative = field(Decimal("-0.01"), source)
    with pytest.raises(ValidationError):
        ExtractedDocument(**document(source, total_amount=negative))


def test_present_value_requires_source() -> None:
    with pytest.raises(ValidationError, match="source"):
        ExtractedField(value="42", confidence=0.8, sources=[])


def test_absent_value_has_zero_confidence() -> None:
    missing = ExtractedField[str](value=None, confidence=0, sources=[])
    assert missing.value is None


def test_spreadsheet_source_requires_cell() -> None:
    with pytest.raises(ValidationError, match="sheet и cell_range"):
        SourceRef(kind=SourceKind.SPREADSHEET, sheet="Лист1")


# ---------------------------------------------------------------------------
# Нормализация валюты
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("руб.", "RUB"),
        ("руб", "RUB"),
        ("₽", "RUB"),
        ("Российский рубль", "RUB"),
        ("российские рубли", "RUB"),
        ("rur", "RUB"),
        ("RUB", "RUB"),
        ("rub", "RUB"),
        (" usd ", "USD"),
        ("$", "USD"),
        ("EUR", "EUR"),
        ("евро", "EUR"),
        ("eur", "EUR"),
    ],
)
def test_normalize_currency_maps_known_aliases(raw: str, expected: str) -> None:
    assert normalize_currency(raw) == expected


def test_normalize_currency_passthrough_unknown() -> None:
    assert normalize_currency("CHF") == "CHF"


@pytest.mark.parametrize(
    "raw",
    [
        "руб.",
        "₽",
        "Российский рубль",
        "usd",
        "$",
        "евро",
    ],
)
def test_currency_field_accepts_normalized_value(raw: str, source: SourceRef) -> None:
    doc = ExtractedDocument(**document(source, currency=field(raw, source)))
    assert doc.currency.value is not None


def test_currency_field_rejects_unknown_value(source: SourceRef) -> None:
    with pytest.raises(ValidationError):
        ExtractedDocument(**document(source, currency=field("xy", source)))


# ---------------------------------------------------------------------------
# Нормализация КПП: ОГРНИП → null
# ---------------------------------------------------------------------------


def test_kpp_9_digits_preserved(source: SourceRef) -> None:
    doc = Party(
        name=field("ИП Иванов", source),
        inn=field("500100732259", source),
        kpp=field("773601001", source),
    )
    assert doc.kpp.value == "773601001"


def test_kpp_15_digit_ogrnip_becomes_none(source: SourceRef) -> None:
    doc = Party(
        name=field("ИП Иванов", source),
        inn=field("500100732259", source),
        kpp=field("318774600123456", source),
    )
    assert doc.kpp.value is None
    assert doc.kpp.confidence == 0
    assert doc.kpp.sources == []


def test_kpp_ogrnip_in_full_document(source: SourceRef) -> None:
    doc = ExtractedDocument(**document(
        source,
        supplier={
            "name": field("ИП Иванов", source),
            "inn": field("500100732259", source),
            "kpp": field("318774600123456", source),
        },
    ))
    assert doc.supplier.kpp.value is None


def test_kpp_invalid_short_value_rejected(source: SourceRef) -> None:
    with pytest.raises(ValidationError):
        Party(
            name=field("ИП Иванов", source),
            inn=field("500100732259", source),
            kpp=field("123", source),
        )


def test_kpp_13_digit_ogrn_becomes_none(source: SourceRef) -> None:
    doc = Party(
        name=field("ООО Контрагент", source),
        inn=field("7707083893", source),
        kpp=field("1027700132195", source),
    )
    assert doc.kpp.value is None
    assert doc.kpp.confidence == 0


# ---------------------------------------------------------------------------
# Канонизация отсутствующего значения на уровне ExtractedField
# ---------------------------------------------------------------------------


def test_missing_value_forces_zero_confidence() -> None:
    missing = ExtractedField[str](value=None, confidence=1.0)
    assert missing.value is None
    assert missing.confidence == 0


def test_missing_value_clears_sources(source: SourceRef) -> None:
    missing = ExtractedField[str].model_validate(field(None, source, confidence=1.0))
    assert missing.value is None
    assert missing.confidence == 0
    assert missing.sources == []


def test_present_value_metadata_preserved(source: SourceRef) -> None:
    present = ExtractedField[str].model_validate(field("42", source, confidence=0.77))
    assert present.value == "42"
    assert present.confidence == 0.77
    assert len(present.sources) == 1
    assert present.sources[0].text == source.text


def test_supplier_kpp_null_with_confidence_one_canonicalized(source: SourceRef) -> None:
    doc = ExtractedDocument(**document(
        source,
        supplier={
            "name": field("ИП Иванов", source),
            "inn": field("500100732259", source),
            "kpp": {"value": None, "confidence": 1.0},
        },
    ))
    assert doc.supplier.kpp.value is None
    assert doc.supplier.kpp.confidence == 0
    assert doc.supplier.kpp.sources == []


def test_vat_amount_null_with_confidence_one_canonicalized(source: SourceRef) -> None:
    doc = ExtractedDocument(**document(
        source,
        vat_amount={"value": None, "confidence": 1.0, "sources": [source.model_dump()]},
    ))
    assert doc.vat_amount.value is None
    assert doc.vat_amount.confidence == 0
    assert doc.vat_amount.sources == []


def test_confidence_epsilon_above_one_snapped(source: SourceRef) -> None:
    """qwen2.5-coder возвращает 1.0000000000000002 — это единица, а не ошибка."""
    present = ExtractedField[str].model_validate(
        field("42", source, confidence=1.0000000000000002)
    )
    assert present.confidence == 1.0


def test_confidence_epsilon_below_zero_snapped(source: SourceRef) -> None:
    present = ExtractedField[str].model_validate(field("42", source, confidence=-1e-13))
    assert present.confidence == 0.0


def test_confidence_inside_range_unchanged(source: SourceRef) -> None:
    present = ExtractedField[str].model_validate(field("42", source, confidence=0.75))
    assert present.confidence == 0.75


@pytest.mark.parametrize("confidence", [1.01, -0.01])
def test_confidence_outside_tolerance_rejected(confidence: float, source: SourceRef) -> None:
    with pytest.raises(ValidationError):
        ExtractedField[str].model_validate(field("42", source, confidence=confidence))


def test_confidence_snapping_does_not_mutate_input(source: SourceRef) -> None:
    payload = field("42", source, confidence=1.0000000000000002)
    ExtractedField[str].model_validate(payload)
    assert payload["confidence"] == 1.0000000000000002


def test_missing_value_canonicalized_despite_epsilon_confidence(source: SourceRef) -> None:
    missing = ExtractedField[str].model_validate(
        field(None, source, confidence=1.0000000000000002)
    )
    assert missing.value is None
    assert missing.confidence == 0
    assert missing.sources == []


def test_canonicalization_does_not_mutate_input(source: SourceRef) -> None:
    payload = {
        "name": field("ИП Иванов", source),
        "inn": field("500100732259", source),
        "kpp": field("318774600123456", source, confidence=1.0),
    }
    Party.model_validate(payload)
    assert payload["kpp"]["value"] == "318774600123456"
    assert payload["kpp"]["confidence"] == 1.0
    assert payload["kpp"]["sources"] != []
