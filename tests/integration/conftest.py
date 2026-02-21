"""Integration test fixtures — require a running Postgres."""

from __future__ import annotations

import os

# Point at the test docker-compose services (port 5499 for DB, 8201 for KMS)
os.environ.setdefault("P8_DATABASE_URL", "postgresql://p8:p8_dev@localhost:5499/p8")
os.environ.setdefault("P8_KMS_VAULT_URL", "http://localhost:8201")

import pytest_asyncio

from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.kms import LocalFileKMS


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
