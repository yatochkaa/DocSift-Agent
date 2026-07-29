from docsift.services.llm.prompt import load_document_extraction_prompt


def test_v4_prompt_defines_iso_date_rule() -> None:
    text = load_document_extraction_prompt("v4").text
    assert "YYYY-MM-DD" in text
    assert "30.06.2026" in text and '"2026-06-30"' in text


def test_v4_prompt_defines_currency_rule() -> None:
    text = load_document_extraction_prompt("v4").text
    assert "ISO 4217" in text
    assert '"RUB"' in text


def test_v4_prompt_defines_kpp_rule() -> None:
    text = load_document_extraction_prompt("v4").text
    assert "9 цифр" in text
    assert "ОГРН/ОГРНИП" in text


def test_v5_prompt_classifies_supported_document_types() -> None:
    text = load_document_extraction_prompt("v5").text
    assert "consignment_note_torg12" in text
    assert "universal_transfer_document" in text
    assert "vat_invoice" in text
    assert "work_completion_act" in text
    assert "payment_invoice" in text
    assert "Товарная накладная" in text


def test_v5_prompt_requires_every_table_row() -> None:
    text = load_document_extraction_prompt("v5").text
    assert "Каждая строка таблицы" in text
    assert "Не возвращай пустой `line_items`" in text


def test_v5_prompt_amount_semantics_match_guardrails_and_export() -> None:
    text = load_document_extraction_prompt("v5").text
    assert "`amount` — сумма строки БЕЗ НДС" in text
    assert "Не помещай «Всего с НДС» в `line_items[].amount`" in text
    assert "`vat_amount` = «Сумма НДС»" in text
    assert "`total_amount` — итоговая сумма документа с НДС" in text
