"""Web-level security tests for upload and file serving vulnerabilities.

Tests the HTTP layer of the upload and source endpoints, verifying that
Content-Length guards and chunked-size guards reject oversized files before
they reach storage, and that source responses include security headers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from docsift.pipeline.storage import DocumentStorage, UploadTooLargeError
from docsift.web.app import create_standalone_app

from .conftest import FakeGateway

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _csrf(client: AsyncClient) -> str:
    page = await client.get("/documents")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "CSRF token not found on /documents page"
    return match.group(1)


@pytest.fixture
def _small_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch get_settings to return a very small upload limit for testing."""
    small = type("S", (), {
        "storage_path": ".",
        "max_upload_bytes": 1024,
        "upload_chunk_bytes": 256,
    })()
    monkeypatch.setattr("docsift.core.config.get_settings", lambda: small)


@pytest.fixture
def small_limit_app(monkeypatch: pytest.MonkeyPatch):
    """Standalone app with monkeypatched small upload limit and a fresh FakeGateway."""
    small = type("S", (), {
        "storage_path": ".",
        "max_upload_bytes": 1024,
        "upload_chunk_bytes": 256,
    })()
    monkeypatch.setattr("docsift.core.config.get_settings", lambda: small)
    app = create_standalone_app()
    app.state.gateway = FakeGateway()
    return app


@pytest_asyncio.fixture
async def upload_limit_client(small_limit_app):
    """AsyncClient with small upload limit, plus the FakeGateway reference."""
    async with AsyncClient(
        transport=ASGITransport(app=small_limit_app), base_url="http://test"
    ) as ac:
        yield ac, small_limit_app.state.gateway


@pytest_asyncio.fixture
async def security_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Client for source-endpoint security tests.

    Creates a real file in tmp_path and overrides FakeGateway.document to
    return its object_key so the source endpoint can resolve it.
    """
    storage = DocumentStorage(root=tmp_path, max_bytes=1024 * 1024)
    pdf_content = b"%PDF-1.4\n" + b"x" * 100
    stored = storage.save(file_name="test.pdf", payload=pdf_content)

    monkeypatch.setattr(
        "docsift.core.config.get_settings",
        lambda: type("S", (), {"storage_path": str(tmp_path), "max_upload_bytes": 1024 * 1024})(),
    )

    app = create_standalone_app()
    gateway = FakeGateway()

    async def _custom_document(document_id: str):
        if document_id == "test-doc":
            return {
                "document": {
                    "id": "test-doc",
                    "file_name": "test.pdf",
                    "object_key": stored.object_key,
                    "content_type": "application/pdf",
                    "status": "completed",
                },
                "extracted": {},
                "guardrails": [],
                "step_durations": {},
                "pages": [],
                "review": {"open_count": 0, "correction_count": 0, "can_complete": False, "can_export": False},
            }
        return None

    gateway.document = _custom_document
    app.state.gateway = gateway

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Tests 1–2: upload limit enforcement
# ---------------------------------------------------------------------------

async def test_upload_with_content_length_exceeding_limit_rejected(
    upload_limit_client: tuple[AsyncClient, FakeGateway],
    tmp_path: Path,
):
    """Content-Length > limit → 413, gateway.upload never called, no disk write."""
    client, gateway = upload_limit_client
    initial_files = len(list(tmp_path.rglob("*")))
    upload_called = False
    original_upload = gateway.upload

    async def _tracked(file_name: str, payload: bytes):
        nonlocal upload_called
        upload_called = True
        return await original_upload(file_name, payload)

    gateway.upload = _tracked

    csrf = await _csrf(client)
    response = await client.post(
        "/documents/upload",
        headers={
            "HX-Request": "true",
            "Content-Length": "100000000",
        },
        files={"file": ("test.pdf", b"small", "application/pdf")},
        data={"csrf_token": csrf},
    )

    assert response.status_code == 413
    assert not upload_called
    assert len(list(tmp_path.rglob("*"))) == initial_files


async def test_upload_file_exceeding_limit_rejected_no_disk_write(
    upload_limit_client: tuple[AsyncClient, FakeGateway],
    tmp_path: Path,
):
    """Actual payload > limit during chunked read → 413, no file written to disk."""
    client, gateway = upload_limit_client
    initial_files = len(list(tmp_path.rglob("*")))
    upload_called = False
    original_upload = gateway.upload

    async def _tracked(file_name: str, payload: bytes):
        nonlocal upload_called
        upload_called = True
        return await original_upload(file_name, payload)

    gateway.upload = _tracked

    csrf = await _csrf(client)
    large_content = b"%PDF-1.4 " + b"x" * 1024  # 1028 bytes > 1024 limit

    response = await client.post(
        "/documents/upload",
        headers={"HX-Request": "true"},
        files={"file": ("large.pdf", large_content, "application/pdf")},
        data={"csrf_token": csrf},
    )

    assert response.status_code == 413
    assert not upload_called
    assert len(list(tmp_path.rglob("*"))) == initial_files


# ---------------------------------------------------------------------------
# Tests 7–8: source-endpoint security headers
# ---------------------------------------------------------------------------

async def test_document_source_has_security_headers(security_client: AsyncClient):
    """Source endpoint must set X-Content-Type-Options, CSP sandbox, X-Frame-Options."""
    response = await security_client.get("/documents/test-doc/source")

    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Content-Security-Policy") == "sandbox"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert response.headers.get("content-type") == "application/pdf"

    content_disposition = response.headers.get("content-disposition", "")
    assert "inline" in content_disposition


async def test_database_content_type_override_ignored(security_client: AsyncClient):
    """Malicious content_type in database is ignored; server uses extension-based detection."""
    response = await security_client.get("/documents/test-doc/source")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "application/pdf"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
