from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docsift.schemas.documents import ExtractedDocument
from docsift.schemas.evals import ExpectedDocument
from docsift.services.evals import DatasetError, evaluate_document, load_dataset


def _source() -> dict[str, Any]:
    return {
        "kind": "pdf_text",
        "page": 1,
        "bbox": None,
        "sheet": None,
        "cell_range": None,
        "text": "source",
    }


def _field(value: Any) -> dict[str, Any]:
    if value is None:
        return {"value": None, "confidence": 0, "sources": []}
    return {"value": value, "confidence": 0.99, "sources": [_source()]}


def _actual_payload() -> dict[str, Any]:
    return {
        "document_type": _field("payment_invoice"),
        "number": _field("INV-001"),
        "date": _field("2026-01-10"),
        "supplier": {
            "name": _field("ООО Ромашка"),
            "inn": _field("7707083893"),
            "kpp": _field("773601001"),
        },
        "buyer": {
            "name": _field("АО Покупатель"),
            "inn": _field("500100732259"),
            "kpp": _field(None),
        },
        "total_amount": _field("1200.00"),
        "vat_amount": _field("200.00"),
        "currency": _field("RUB"),
        "line_items": [
            {
                "name": _field("Бумага офисная А4"),
                "quantity": _field("2"),
                "unit": _field("упак"),
                "unit_price": _field("500.00"),
                "amount": _field("1000.00"),
                "vat_rate": _field("20"),
                "vat_amount": _field("166.67"),
            },
            {
                "name": _field("Доставка"),
                "quantity": _field("1"),
                "unit": _field("усл"),
                "unit_price": _field("200.00"),
                "amount": _field("200.00"),
                "vat_rate": _field("20"),
                "vat_amount": _field("33.33"),
            },
        ],
    }


def _expected_payload() -> dict[str, Any]:
    return {
        "document_type": "payment_invoice",
        "number": "INV-001",
        "date": "2026-01-10",
        "supplier": {"name": "ООО «Ромашка»", "inn": "7707083893", "kpp": "773601001"},
        "buyer": {"name": "АО Покупатель", "inn": "500100732259", "kpp": None},
        "total_amount": "1200.0",
        "vat_amount": "200.00",
        "currency": "RUB",
        "line_items": [
            {
                "name": "Доставка",
                "quantity": "1.0",
                "unit": "усл",
                "unit_price": "200",
                "amount": "200.00",
                "vat_rate": "20.0",
                "vat_amount": "33.33",
            },
            {
                "name": "Бумага офисная A4",
                "quantity": "2.000",
                "unit": "упак",
                "unit_price": "500.000",
                "amount": "1000",
                "vat_rate": "20",
                "vat_amount": "166.67",
            },
        ],
    }


def test_metrics_use_field_specific_comparison_and_match_reordered_items() -> None:
    metrics = evaluate_document(
        ExpectedDocument.model_validate(_expected_payload()),
        ExtractedDocument.model_validate(_actual_payload()),
    )

    assert metrics.fields["total_amount"].matches == 1
    assert metrics.fields["supplier.name"].matches == 1
    assert metrics.fields["line_items[].amount"].matches == 2
    assert metrics.fields["line_items[].name"].matches == 2
    assert metrics.fields["line_items[].amount"].misses == 0
    assert metrics.fields["line_items[].amount"].hallucinations == 0


def test_metrics_distinguish_missing_hallucination_and_mismatch() -> None:
    expected_payload = _expected_payload()
    expected_payload["number"] = "INV-002"
    expected_payload["buyer"]["kpp"] = "770101001"
    expected_payload["currency"] = None
    actual_payload = _actual_payload()
    actual_payload["buyer"]["kpp"] = _field(None)
    actual_payload["currency"] = _field("RUB")

    metrics = evaluate_document(
        ExpectedDocument.model_validate(expected_payload),
        ExtractedDocument.model_validate(actual_payload),
    )

    assert metrics.fields["number"].mismatches == 1
    assert metrics.fields["buyer.kpp"].misses == 1
    assert metrics.fields["currency"].hallucinations == 1


def test_unmatched_line_items_become_misses_and_hallucinations() -> None:
    expected_payload = _expected_payload()
    expected_payload["line_items"] = [expected_payload["line_items"][0]]
    actual_payload = _actual_payload()
    actual_payload["line_items"][0]["name"] = _field("Несвязанный товар")
    actual_payload["line_items"][0]["amount"] = _field("999.00")
    actual_payload["line_items"][1]["name"] = _field("Несвязанная услуга")

    metrics = evaluate_document(
        ExpectedDocument.model_validate(expected_payload),
        ExtractedDocument.model_validate(actual_payload),
        line_item_match_threshold=0.9,
    )

    assert metrics.fields["line_items[].name"].misses == 1
    assert metrics.fields["line_items[].name"].hallucinations == 2


def test_dataset_loader_pairs_document_and_expected_json(tmp_path: Path) -> None:
    dataset_root = tmp_path / "dataset"
    samples = dataset_root / "samples" / "payment_invoice"
    samples.mkdir(parents=True)
    (dataset_root / "manifest.json").write_text(
        json.dumps({"name": "accounting", "version": "v1"}), encoding="utf-8"
    )
    (samples / "pi-0001.pdf").write_bytes(b"%PDF-1.7")
    (samples / "pi-0001.expected.json").write_text(
        json.dumps(_expected_payload(), ensure_ascii=False), encoding="utf-8"
    )

    dataset = load_dataset(dataset_root)

    assert dataset.manifest.name == "accounting"
    assert dataset.samples[0].sample_id == "pi-0001"
    assert dataset.samples[0].document_path.name == "pi-0001.pdf"


def test_dataset_loader_rejects_sample_without_document(tmp_path: Path) -> None:
    dataset_root = tmp_path / "dataset"
    samples = dataset_root / "samples"
    samples.mkdir(parents=True)
    (dataset_root / "manifest.json").write_text(
        json.dumps({"name": "accounting", "version": "v1"}), encoding="utf-8"
    )
    (samples / "pi-0001.expected.json").write_text(
        json.dumps(_expected_payload(), ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(DatasetError, match="exactly one document"):
        load_dataset(dataset_root)
