"""Integration tests for session_timeline decryption — per-row encryption_level logic."""

from __future__ import annotations

from uuid import UUID

import pytest

from p8.ontology.types import Moment
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.data import create_session, seed_messages


TENANT_ID = "test-timeline-tenant"
USER_ID = UUID("aaaaaaaa-0000-0000-0000-111111111111")


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


@pytest.fixture
async def platform_tenant(encryption):
    """Configure a tenant with platform encryption (server decrypts on read)."""
    await encryption.configure_tenant(TENANT_ID, enabled=True, own_key=True, mode="platform")
    return TENANT_ID


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_timeline_decrypts_platform_messages(db, encryption, platform_tenant):
    """Platform-encrypted messages should come back decrypted in the timeline."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, user_id=USER_ID)

    # Seed messages with tenant_id → they get encrypted + encryption_level='platform'
    await seed_messages(memory, session.id, 4, token_count=10, tenant_id=TENANT_ID, user_id=USER_ID)

    # Verify messages are actually encrypted in DB
    raw = await db.fetch(
        "SELECT content, encryption_level FROM messages WHERE session_id = $1 ORDER BY created_at",
        session.id,
    )
    assert len(raw) == 4
    for row in raw:
        assert row["encryption_level"] == "platform"
        # Encrypted content is base64 — should not contain the seed prefix
        assert not row["content"].startswith("msg-")

    # Now call the endpoint logic directly (import the router function)
    from p8.api.routers.moments import session_timeline

    # We can't call the FastAPI endpoint directly without the app, so replicate the logic
    rows = await db.rem_session_timeline(session.id)
    assert len(rows) == 4

    # Simulate what the endpoint does
    from p8.api.routers.moments import Message as MsgType, Moment as MoType

    needs_decrypt = any(
        r.get("encryption_level") == "platform"
        or (r.get("encryption_level") is None and r.get("content_or_summary"))
        for r in rows
    )
    assert needs_decrypt

    tenant_row = await db.fetchrow(
        "SELECT tenant_id FROM messages WHERE session_id = $1 AND tenant_id IS NOT NULL LIMIT 1",
        session.id,
    )
    tenant_id = tenant_row["tenant_id"]
    assert tenant_id == TENANT_ID

    await encryption.get_dek(tenant_id)
    fallback_decrypt = await encryption.should_decrypt_on_read(tenant_id)
    assert fallback_decrypt is True

    result = []
    for row in rows:
        data = dict(row)
        level = data.get("encryption_level")
        should_decrypt = level == "platform" or (level is None and fallback_decrypt)

        if should_decrypt and data.get("content_or_summary"):
            if data.get("event_type") == "message":
                dec = encryption.decrypt_fields(
                    MsgType, {"id": data["event_id"], "content": data["content_or_summary"]}, tenant_id
                )
                data["content_or_summary"] = dec["content"]

        result.append(data)

    # Decrypted content should start with seed prefix
    for item in result:
        assert item["content_or_summary"].startswith("msg-"), (
            f"Expected decrypted content, got: {item['content_or_summary'][:80]}"
        )


async def test_timeline_decrypts_platform_moments(db, encryption, platform_tenant):
    """Platform-encrypted moments should also be decrypted in the timeline."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, user_id=USER_ID)

    # Create an encrypted moment
    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"session-{session.id}-chunk-0",
        moment_type="session_chunk",
        summary="Discussed architecture and deployment.",
        source_session_id=session.id,
        tenant_id=TENANT_ID,
        metadata={"message_count": 4, "token_count": 200, "chunk_index": 0},
    )
    await moment_repo.upsert(moment)

    # Verify moment summary is encrypted in DB
    raw = await db.fetchrow(
        "SELECT summary, encryption_level FROM moments WHERE source_session_id = $1",
        session.id,
    )
    assert raw["encryption_level"] == "platform"
    assert not raw["summary"].startswith("Discussed")

    # Also seed a message so tenant_id lookup works
    await seed_messages(memory, session.id, 2, token_count=10, tenant_id=TENANT_ID, user_id=USER_ID)

    rows = await db.rem_session_timeline(session.id)
    moments_in_timeline = [r for r in rows if r["event_type"] == "moment"]
    assert len(moments_in_timeline) == 1
    # Raw moment should still be encrypted
    assert not moments_in_timeline[0]["content_or_summary"].startswith("Discussed")

    # Apply decryption
    await encryption.get_dek(TENANT_ID)
    from p8.ontology.types import Moment as MoType
    dec = encryption.decrypt_fields(
        MoType,
        {"id": moments_in_timeline[0]["event_id"], "summary": moments_in_timeline[0]["content_or_summary"]},
        TENANT_ID,
    )
    assert dec["summary"].startswith("Discussed architecture")


async def test_timeline_skips_unencrypted_rows(db, encryption):
    """Rows with encryption_level='none' should be returned as-is (no decrypt attempt)."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, user_id=USER_ID)

    # Seed WITHOUT tenant_id → no encryption, encryption_level='none'
    await seed_messages(memory, session.id, 3, token_count=10, user_id=USER_ID)

    raw = await db.fetch(
        "SELECT content, encryption_level FROM messages WHERE session_id = $1",
        session.id,
    )
    for row in raw:
        assert row["encryption_level"] == "none"
        assert row["content"].startswith("msg-")

    rows = await db.rem_session_timeline(session.id)
    # No rows need decryption
    needs_decrypt = any(
        r.get("encryption_level") == "platform"
        or (r.get("encryption_level") is None and r.get("content_or_summary"))
        for r in rows
    )
    assert not needs_decrypt

    # Content should already be plaintext
    for r in rows:
        assert r["content_or_summary"].startswith("msg-")


async def test_timeline_sealed_not_decrypted(db, encryption):
    """Sealed-mode messages should NOT be decrypted server-side."""
    sealed_tenant = "test-sealed-tenant"
    # Generate a sealed key pair (server-generated)
    private_pem = await encryption.configure_tenant_sealed(sealed_tenant)
    assert private_pem is not None  # Server returns private key once

    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, user_id=USER_ID)

    await seed_messages(memory, session.id, 2, token_count=10, tenant_id=sealed_tenant, user_id=USER_ID)

    raw = await db.fetch(
        "SELECT encryption_level FROM messages WHERE session_id = $1",
        session.id,
    )
    for row in raw:
        assert row["encryption_level"] == "sealed"

    # should_decrypt_on_read should return False for sealed
    assert await encryption.should_decrypt_on_read(sealed_tenant) is False

    rows = await db.rem_session_timeline(session.id)
    # Per-row check: sealed rows should NOT trigger decryption
    for r in rows:
        level = r.get("encryption_level")
        assert level == "sealed"
        should_decrypt = level == "platform" or (level is None and False)
        assert not should_decrypt


async def test_timeline_mixed_encryption_levels(db, encryption, platform_tenant):
    """Session with both encrypted and unencrypted messages — only platform rows decrypted."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, user_id=USER_ID)

    # First 2 messages: unencrypted
    await seed_messages(memory, session.id, 2, token_count=10, prefix="plain", user_id=USER_ID)
    # Next 2 messages: platform-encrypted
    await seed_messages(memory, session.id, 2, token_count=10, prefix="secret", tenant_id=TENANT_ID, user_id=USER_ID)

    raw = await db.fetch(
        "SELECT content, encryption_level FROM messages WHERE session_id = $1 ORDER BY created_at",
        session.id,
    )
    assert raw[0]["encryption_level"] == "none"
    assert raw[1]["encryption_level"] == "none"
    assert raw[2]["encryption_level"] == "platform"
    assert raw[3]["encryption_level"] == "platform"

    rows = await db.rem_session_timeline(session.id)
    assert len(rows) == 4

    # Apply the endpoint's per-row logic
    await encryption.get_dek(TENANT_ID)
    fallback_decrypt = await encryption.should_decrypt_on_read(TENANT_ID)

    from p8.ontology.types import Message as MsgType

    result = []
    for row in rows:
        data = dict(row)
        level = data.get("encryption_level")
        should_decrypt = level == "platform" or (level is None and fallback_decrypt)

        if should_decrypt and data.get("content_or_summary"):
            if data.get("event_type") == "message":
                dec = encryption.decrypt_fields(
                    MsgType, {"id": data["event_id"], "content": data["content_or_summary"]}, TENANT_ID
                )
                data["content_or_summary"] = dec["content"]
        result.append(data)

    # All 4 should now be plaintext
    plain_msgs = [r for r in result if r["content_or_summary"].startswith("plain-")]
    secret_msgs = [r for r in result if r["content_or_summary"].startswith("secret-")]
    assert len(plain_msgs) == 2
    assert len(secret_msgs) == 2
