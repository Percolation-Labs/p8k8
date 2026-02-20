"""Integration tests for Vault Transit KMS — platform and client encryption modes.

OpenBao (Vault-compatible) runs as the `kms` service in docker-compose.yml on port 8200.
Start all services with: docker compose up -d
These tests MUST NOT be skipped — if OpenBao is unreachable the fixture will fail hard.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from p8.ontology.types import Message, User
from p8.services.encryption import EncryptionService
from p8.services.kms import VaultTransitKMS
from p8.services.repository import Repository

# OpenBao dev-mode settings from docker-compose.yml
VAULT_URL = "http://localhost:8200"
VAULT_TOKEN = "dev-root-token"
VAULT_KEY = "p8-master"


def det_id(name: str) -> UUID:
    """Deterministic UUID from a name for idempotent inserts."""
    return UUID(hashlib.md5(name.encode()).hexdigest())


@pytest.fixture(scope="module")
def vault_available():
    """Ensure OpenBao (kms service in docker-compose.yml) is reachable and Transit engine is ready."""
    import httpx

    resp = httpx.get(f"{VAULT_URL}/v1/sys/health", timeout=2)
    assert resp.status_code in (200, 429, 472, 473), (
        f"OpenBao not healthy (status {resp.status_code}). Run: docker compose up -d"
    )

    headers = {"X-Vault-Token": VAULT_TOKEN}
    # Enable transit engine (idempotent — 204 if already enabled, 200 on first)
    httpx.post(
        f"{VAULT_URL}/v1/sys/mounts/transit",
        headers=headers,
        json={"type": "transit"},
        timeout=5,
    )
    # Create master key (idempotent — 204 if exists)
    httpx.post(
        f"{VAULT_URL}/v1/transit/keys/{VAULT_KEY}",
        headers=headers,
        json={"type": "aes256-gcm96"},
        timeout=5,
    )


@pytest_asyncio.fixture
async def vault_env(db, vault_available):
    """Vault KMS environment — independent of LocalFileKMS fixtures."""
    # Clean vault-specific tenant keys so they don't poison LocalFileKMS tests
    for t in ("vault-platform-tenant", "vault-client-tenant", "vault-fallback-tenant",
              "vault-iso-a", "vault-iso-b", "__system__"):
        await db.execute("DELETE FROM tenant_keys WHERE tenant_id = $1", t)

    kms = VaultTransitKMS(VAULT_URL, VAULT_TOKEN, VAULT_KEY, db)
    enc = EncryptionService(kms, system_tenant_id="__system__", cache_ttl=300)
    await enc.ensure_system_key()
    yield enc
    # Clean up Vault-wrapped keys so LocalFileKMS tests aren't poisoned
    for t in ("vault-platform-tenant", "vault-client-tenant", "vault-fallback-tenant",
              "vault-iso-a", "vault-iso-b", "__system__"):
        await db.execute("DELETE FROM tenant_keys WHERE tenant_id = $1", t)


@pytest.mark.asyncio
async def test_vault_platform_mode(db, vault_env, vault_available):
    """Platform mode with Vault: encrypt at rest, decrypt on read."""
    enc = vault_env
    tenant = "vault-platform-tenant"
    await enc.configure_tenant(tenant, enabled=True, own_key=True, mode="platform")

    session_id = det_id("vault-platform-session")
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
        session_id, "vault-platform-session", tenant,
    )

    repo = Repository(Message, db, enc)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Secret via Vault platform mode",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    # DB has ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] != "Secret via Vault platform mode"

    # Platform mode: server decrypts
    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    assert loaded.content == "Secret via Vault platform mode"


@pytest.mark.asyncio
async def test_vault_client_mode(db, vault_env, vault_available):
    """Client mode with Vault: encrypt at rest, API returns ciphertext."""
    enc = vault_env
    tenant = "vault-client-tenant"
    await enc.configure_tenant(tenant, enabled=True, own_key=True, mode="client")

    session_id = det_id("vault-client-session")
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id",
        session_id, "vault-client-session", tenant,
    )

    repo = Repository(Message, db, enc)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Secret via Vault client mode",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    # DB has ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] != "Secret via Vault client mode"

    # Client mode: API returns ciphertext
    loaded_raw = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    assert loaded_raw.content != "Secret via Vault client mode"
    assert loaded_raw.content == raw["content"]

    # Client-side: force decrypt to simulate client having key access
    loaded_dec = await repo.get(saved.id, tenant_id=tenant, decrypt=True)
    assert loaded_dec.content == "Secret via Vault client mode"


@pytest.mark.asyncio
async def test_vault_system_key_fallback(db, vault_env, vault_available):
    """Tenant without own key falls back to system DEK via Vault."""
    enc = vault_env
    tenant = "vault-fallback-tenant"

    repo = Repository(User, db, enc)
    user = User(name="Vault Fallback", content="Bio encrypted by system key", tenant_id=tenant)
    [saved] = await repo.upsert(user)

    # Encrypted in DB
    raw = await db.fetchrow("SELECT content FROM users WHERE id = $1", saved.id)
    assert raw["content"] != "Bio encrypted by system key"

    # Decrypts with system DEK
    loaded = await repo.get(saved.id, tenant_id=tenant)
    assert loaded.content == "Bio encrypted by system key"


@pytest.mark.asyncio
async def test_vault_tenant_isolation(db, vault_env, vault_available):
    """Tenants with own Vault keys can't read each other's data."""
    enc = vault_env
    tenant_a = "vault-iso-a"
    tenant_b = "vault-iso-b"
    await enc.configure_tenant(tenant_a, enabled=True, own_key=True)
    await enc.configure_tenant(tenant_b, enabled=True, own_key=True)

    repo = Repository(User, db, enc)
    user = User(name="Vault Isolated", content="Only for tenant A", tenant_id=tenant_a)
    [saved] = await repo.upsert(user)

    # Wrong tenant can't decrypt
    loaded_wrong = await repo.get(saved.id, tenant_id=tenant_b)
    assert loaded_wrong.content != "Only for tenant A"

    # Right tenant works
    loaded_right = await repo.get(saved.id, tenant_id=tenant_a)
    assert loaded_right.content == "Only for tenant A"
