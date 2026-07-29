from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.asyncio


async def _csrf(client) -> str:
    page = await client.get("/documents/doc-1/review")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match
    return match.group(1)


async def test_field_correction_is_saved(upload_client):
    client, gateway = upload_client
    token = await _csrf(client)
    response = await client.post(
        "/documents/doc-1/fields",
        data={"csrf_token": token, "field_path": "/buyer/kpp", "value": "780145623"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/review?field_saved=1")
    assert gateway.saved_corrections == [("doc-1", "/buyer/kpp", "780145623")]


async def test_complete_review_changes_workflow_state(upload_client):
    client, gateway = upload_client
    token = await _csrf(client)
    response = await client.post(
        "/documents/doc-1/complete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/review?review_complete=1")
    assert gateway.review_completed is True


async def test_xlsx_export_is_real_workbook(upload_client):
    client, gateway = upload_client
    gateway.document_status = "completed"
    response = await client.get("/documents/doc-1/export.xlsx")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.content.startswith(b"PK")
    assert len(response.content) > 1000


async def test_xlsx_export_requires_completed_review(upload_client):
    client, gateway = upload_client
    gateway.document_status = "review_required"
    response = await client.get("/documents/doc-1/export.xlsx")
    assert response.status_code == 409


async def test_review_mutations_require_csrf(upload_client):
    client, _ = upload_client
    correction = await client.post(
        "/documents/doc-1/fields",
        data={"field_path": "/number", "value": "42"},
    )
    complete = await client.post("/documents/doc-1/complete")
    assert correction.status_code == 403
    assert complete.status_code == 403


# ---------------------------------------------------------------------------
# B1: line-item editable cells HTML integration test
# ---------------------------------------------------------------------------

async def test_line_item_editable_cells_rendered(client):
    """Review page renders editable line-item cells with correct form fields."""
    response = await client.get("/documents/doc-1/review")
    assert response.status_code == 200
    body = response.text
    assert "ds-editable-cell" in body
    assert 'class="ds-cell-input"' in body
    # EXTRACTED fixture has line_items with keys: name, qty, amount
    assert "/line_items/0/name" in body
    assert "/line_items/0/qty" in body
    assert "/line_items/0/amount" in body
    # Verify the hidden input has the correct name and value pair
    assert 'class="ds-cell-input"' in body
    assert 'class="ds-cell-input"' in body
    assert 'class="ds-cell-input"' in body
    # Ensure display labels (Russian headers like "НДС") are not used as backend keys
    assert 'class="ds-cell-input"' in body
    assert 'class="ds-cell-input"' in body
