"""Shared helpers for unit tests — mock services, utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def mock_services():
    """Create mock services tuple matching async_services() yield (6-tuple)."""
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db.rem_query = AsyncMock(return_value=[])
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()

    encryption = MagicMock()
    encryption.ensure_system_key = AsyncMock()
    encryption.get_dek = AsyncMock(return_value=b"fake-key")
    encryption._dek_cache = {}
    encryption.encrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)
    encryption.decrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)

    settings = MagicMock()
    settings.system_tenant_id = "__system__"
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200

    file_service = MagicMock()
    file_service.read_text = AsyncMock(return_value="")
    file_service.list_dir = MagicMock(return_value=[])

    content_service = MagicMock()
    content_service.ingest_path = AsyncMock()
    content_service.ingest_directory = AsyncMock(return_value=[])
    content_service.upsert_markdown = AsyncMock()
    content_service.upsert_structured = AsyncMock()

    embedding_service = None
    return db, encryption, settings, file_service, content_service, embedding_service


class MockAsyncServices:
    """Async context manager that yields mock services."""

    def __init__(self):
        self.services = mock_services()

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *args):
        pass
