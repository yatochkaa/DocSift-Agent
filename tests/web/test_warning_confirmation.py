"""HTTP acceptance tests for manual confirmation of guardrail warnings."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from docsift.domain.enums import DocumentStatus


pytestmark = pytest.mark.asyncio


async def _csrf(client) -> str:
    page = await client.get("/documents/doc-1/review")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


def _field(value, *, confidence: float = 1.0) -> dict:
    return {
        "value": value,
        "confidence": confidence if value is not None else 0.0,
        "sources": (
            [{"kind": "pdf_text", "page": 1, "text": "warning confirmation fixture"}]
            if value is not None
            else []
        ),
    }


def _extraction(*, invalid_inn: bool = True, same_parties: bool = False) -> dict:
    supplier_valid_inn = "7736050003"
    buyer_valid_inn = "7707083893"
    supplier_name = "Test Supplier"
    buyer_name = supplier_name if same_parties else "Test Buyer"
    supplier_inn = (
        buyer_valid_inn
        if same_parties
        else (supplier_valid_inn if not invalid_inn else "9999999999")
    )
    buyer_inn = buyer_valid_inn
    return {
        "document_type": _field("payment_invoice"),
        "number": _field("123"),
        "date": _field(date.today()),
        "supplier": {
            "name": _field(supplier_name),
            "inn": _field(supplier_inn),
            "kpp": _field(None),
        },
        "buyer": {
            "name": _field(buyer_name),
            "inn": _field(buyer_inn),
            "kpp": _field(None),
        },
        "total_amount": _field(Decimal("1200.00")),
        "vat_amount": _field(Decimal("200.00")),
        "currency": _field("RUB"),
        "line_items": [
            {
                "name": _field("Item 1"),
                "quantity": _field(Decimal("1")),
                "unit": _field("С€С‚"),
                "unit_price": _field(Decimal("1000.00")),
                "amount": _field(Decimal("1000.00")),
                "vat_rate": _field(Decimal("20")),
                "vat_amount": _field(Decimal("200.00")),
            }
        ],
    }


class TestWarningConfirmation:
    async def test_warning_without_confirmation_blocks_completion(self, upload_client):
        client, gateway = upload_client
        gateway.extraction_result = _extraction()

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client)},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "requires_confirmation=1" in response.headers["location"]
        assert gateway.review_completed is False

    async def test_warning_with_confirmation_allows_completion(self, upload_client):
        client, gateway = upload_client
        gateway.extraction_result = _extraction()

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client), "confirm_warnings": True},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "review_complete=1" in response.headers["location"]
        assert "warnings_confirmed=1" in response.headers["location"]
        assert gateway.review_completed is True
        assert gateway.document_status == DocumentStatus.COMPLETED

    async def test_guardrail_error_allows_explicit_human_approval(self, upload_client):
        client, gateway = upload_client
        gateway.document_status = DocumentStatus.REVIEW_REQUIRED
        gateway.extraction_result = _extraction(invalid_inn=False, same_parties=True)

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client), "confirm_warnings": True},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "review_complete=1" in response.headers["location"]
        assert gateway.review_completed is True
        assert gateway.document_status == DocumentStatus.COMPLETED

    async def test_double_click_does_not_create_double_completion(self, upload_client):
        client, gateway = upload_client
        gateway.extraction_result = _extraction()
        token = await _csrf(client)

        first = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": token, "confirm_warnings": True},
            follow_redirects=False,
        )
        assert first.status_code == 303
        first_timestamp = gateway.completion_timestamp
        first_audit_count = len(gateway.review_tasks)

        second = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": token, "confirm_warnings": True},
            follow_redirects=False,
        )

        assert second.status_code == 303
        assert gateway.completion_timestamp == first_timestamp
        assert len(gateway.review_tasks) == first_audit_count

    async def test_raw_extraction_result_not_modified(self, upload_client):
        client, gateway = upload_client
        original = _extraction()
        gateway.extraction_result = deepcopy(original)

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client), "confirm_warnings": True},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert gateway.review_completed is True
        assert gateway.extraction_result == original

    async def test_xlsx_available_after_confirmed_completion(self, upload_client):
        client, gateway = upload_client
        gateway.extraction_result = _extraction()

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client), "confirm_warnings": True},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert gateway.document_status == DocumentStatus.COMPLETED

        xlsx_response = await client.get("/documents/doc-1/export.xlsx")
        assert xlsx_response.status_code == 200
        assert xlsx_response.content.startswith(b"PK")
        assert len(xlsx_response.content) > 1000

    async def test_audit_trail_for_confirmed_warnings(self, upload_client):
        client, gateway = upload_client
        gateway.extraction_result = _extraction()

        response = await client.post(
            "/documents/doc-1/complete",
            data={"csrf_token": await _csrf(client), "confirm_warnings": True},
            follow_redirects=False,
        )

        assert response.status_code == 303
        audit_tasks = [
            task
            for task in gateway.review_tasks
            if task.get("field_path") == "/review_completion"
        ]
        assert len(audit_tasks) == 1
        assert "manual_confirmation" in audit_tasks[0]["reason"]
        assert audit_tasks[0]["original_value"] == "issues_confirmed"
        assert audit_tasks[0]["status"] == "resolved"
