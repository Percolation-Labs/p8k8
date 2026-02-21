"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
import sys
from uuid import UUID

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Force test-safe settings before any module imports Settings()
os.environ["P8_EMBEDDING_MODEL"] = "local"
os.environ["P8_EMBEDDING_WORKER_ENABLED"] = "false"
os.environ["P8_API_KEY"] = ""  # disable auth middleware in tests
os.environ["P8_OTEL_ENABLED"] = "false"  # disable OTEL in tests

from p8.ontology.base import deterministic_id  # noqa: E402
from p8.services.database import Database  # noqa: E402
from p8.services.encryption import EncryptionService  # noqa: E402
from p8.services.kms import LocalFileKMS  # noqa: E402
from p8.settings import Settings  # noqa: E402


def det_id(table: str, name: str) -> UUID:
    """Deterministic UUID5 from table+name for idempotent test inserts."""
    return deterministic_id(table, name)


@pytest.fixture(scope="session")
def settings():
    return Settings()


@pytest_asyncio.fixture
async def db(settings):
    database = Database(settings)
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def encryption(db, settings):
    # Clear any system key left by a different KMS provider (e.g. Vault tests)
    await db.execute(
        "DELETE FROM tenant_keys WHERE tenant_id = $1", settings.system_tenant_id
    )
    kms = LocalFileKMS(settings.kms_local_keyfile, db)
    enc = EncryptionService(kms, system_tenant_id=settings.system_tenant_id, cache_ttl=settings.dek_cache_ttl)
    await enc.ensure_system_key()
    return enc


@pytest_asyncio.fixture
async def clean_db(db, encryption):
    """No-op — tests are idempotent. Use `docker compose down -v` to reset."""
    yield
