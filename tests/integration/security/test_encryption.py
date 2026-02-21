"""Tests for encryption modes: platform, client, disabled, fallback, isolation."""

from __future__ import annotations

from uuid import uuid4

import pytest

from p8.ontology.types import Message, User
from p8.services.repository import Repository


# --- Platform mode (our key, decrypt on read) ---


@pytest.mark.asyncio
async def test_platform_mode_roundtrip(db, encryption, clean_db):
    """Platform mode: encrypted at rest, decrypted transparently on API read."""
    tenant = "platform-tenant"
    await encryption.get_dek(tenant)

    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "platform-session", tenant,
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="This is secret content that should be encrypted",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    # DB has ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] != "This is secret content that should be encrypted"

    # Platform mode: get() decrypts automatically
    loaded = await repo.get(saved.id, tenant_id=tenant)
    assert loaded.content == "This is secret content that should be encrypted"

    # get_for_tenant also decrypts (mode=platform by default)
    loaded2 = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    assert loaded2.content == "This is secret content that should be encrypted"


# --- Client mode (tenant key, ciphertext returned via API) ---


@pytest.mark.asyncio
async def test_client_mode_returns_ciphertext(db, encryption, clean_db):
    """Client mode: encrypted at rest, API returns ciphertext. Client decrypts."""
    tenant = "client-tenant"
    await encryption.configure_tenant(tenant, enabled=True, own_key=True, mode="client")

    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "client-session", tenant,
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Client-encrypted content",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    # DB has ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] != "Client-encrypted content"

    # Mode-aware get: client mode → ciphertext returned
    loaded_raw = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    assert loaded_raw.content != "Client-encrypted content"
    assert loaded_raw.content == raw["content"]  # matches DB ciphertext

    # Client can still force decrypt if they have the key context
    loaded_dec = await repo.get(saved.id, tenant_id=tenant, decrypt=True)
    assert loaded_dec.content == "Client-encrypted content"


@pytest.mark.asyncio
async def test_client_mode_find(db, encryption, clean_db):
    """Client mode find returns ciphertext for all results."""
    tenant = "client-find-tenant"
    await encryption.configure_tenant(tenant, enabled=True, own_key=True, mode="client")

    # Clean up stale test users from prior runs
    await db.execute("DELETE FROM users WHERE tenant_id = $1", tenant)

    repo = Repository(User, db, encryption)
    for name in ["Alice", "Bob"]:
        user = User(name=name, content=f"Bio for {name}", tenant_id=tenant)
        await repo.upsert(user)

    # Mode-aware find: client mode → ciphertext
    results = await repo.find_for_tenant(tenant_id=tenant)
    assert len(results) == 2
    for r in results:
        assert r.content != "Bio for Alice"
        assert r.content != "Bio for Bob"

    # Force decrypt
    results_dec = await repo.find(tenant_id=tenant, decrypt=True)
    contents = {r.content for r in results_dec}
    assert "Bio for Alice" in contents
    assert "Bio for Bob" in contents


# --- Deterministic encryption ---


@pytest.mark.asyncio
async def test_deterministic_encryption(db, encryption, clean_db):
    """Same email + same key → decryptable. AAD includes entity_id."""
    tenant = "det-tenant"
    await encryption.get_dek(tenant)

    repo = Repository(User, db, encryption)
    u1 = User(name="Alice", email="alice@example.com", tenant_id=tenant)
    u2 = User(name="Bob", email="alice@example.com", tenant_id=tenant)

    [saved1] = await repo.upsert(u1)
    [saved2] = await repo.upsert(u2)

    loaded1 = await repo.get(saved1.id, tenant_id=tenant)
    loaded2 = await repo.get(saved2.id, tenant_id=tenant)
    assert loaded1.email == "alice@example.com"
    assert loaded2.email == "alice@example.com"


# --- Tenant isolation ---


@pytest.mark.asyncio
async def test_tenant_isolation(db, encryption, clean_db):
    """Tenant A's DEK cannot decrypt tenant B's content."""
    tenant_a = "iso-tenant-a"
    tenant_b = "iso-tenant-b"
    await encryption.configure_tenant(tenant_a, enabled=True, own_key=True)
    await encryption.configure_tenant(tenant_b, enabled=True, own_key=True)

    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "iso-session", tenant_a,
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Tenant A secret",
        tenant_id=tenant_a,
    )
    [saved] = await repo.upsert(msg)

    # Wrong tenant can't decrypt
    loaded_wrong = await repo.get(saved.id, tenant_id=tenant_b)
    assert loaded_wrong.content != "Tenant A secret"

    # Correct tenant works
    loaded_right = await repo.get(saved.id, tenant_id=tenant_a)
    assert loaded_right.content == "Tenant A secret"


# --- Disabled encryption ---


@pytest.mark.asyncio
async def test_disabled_encryption(db, encryption, clean_db):
    """Tenant that disables encryption stores plaintext."""
    tenant = "disabled-tenant"
    await encryption.configure_tenant(tenant, enabled=False)

    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "disabled-session", tenant,
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="I chose plaintext",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] == "I chose plaintext"


# --- No tenant = no encryption ---


@pytest.mark.asyncio
async def test_no_encryption_without_tenant(db, encryption, clean_db):
    """Without tenant_id, content is stored as plaintext."""
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)", session_id, "no-enc-session"
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Plaintext content",
    )
    [saved] = await repo.upsert(msg)

    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] == "Plaintext content"


# --- System key fallback ---


@pytest.mark.asyncio
async def test_system_key_fallback(db, encryption, clean_db):
    """Tenant with no own key falls back to system DEK — content is encrypted."""
    tenant = "fallback-tenant"

    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "fallback-session", tenant,
    )

    repo = Repository(Message, db, encryption)
    msg = Message(
        session_id=session_id,
        message_type="user",
        content="Encrypted with system key",
        tenant_id=tenant,
    )
    [saved] = await repo.upsert(msg)

    # Encrypted in DB
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", saved.id)
    assert raw["content"] != "Encrypted with system key"

    # Decrypts fine (system DEK)
    loaded = await repo.get(saved.id, tenant_id=tenant)
    assert loaded.content == "Encrypted with system key"


# --- Own key with platform mode ---


@pytest.mark.asyncio
async def test_tenant_own_key_platform(db, encryption, clean_db):
    """Tenant with own key in platform mode: isolated from system DEK, transparent decrypt."""
    tenant = "own-key-tenant"
    await encryption.configure_tenant(tenant, enabled=True, own_key=True, mode="platform")

    repo = Repository(User, db, encryption)
    user = User(name="OwnKey User", content="Private bio", tenant_id=tenant)
    [saved] = await repo.upsert(user)

    # Encrypted in DB
    raw = await db.fetchrow("SELECT content FROM users WHERE id = $1", saved.id)
    assert raw["content"] != "Private bio"

    # Platform mode: decrypted on read
    loaded = await repo.get_for_tenant(saved.id, tenant_id=tenant)
    assert loaded.content == "Private bio"

    # System tenant can't decrypt (different DEK)
    loaded_sys = await repo.get(saved.id, tenant_id=encryption.system_tenant_id)
    assert loaded_sys.content != "Private bio"
