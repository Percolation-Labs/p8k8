"""Tests for message loading, compaction, and moment injection."""

from __future__ import annotations

from uuid import uuid4

import pytest

from p8.ontology.types import Moment
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.tokens import estimate_tokens


@pytest.mark.asyncio
async def test_persist_and_load(db, encryption, clean_db):
    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)", session_id, "mem-session"
    )

    await mem.persist_message(session_id, "user", "Hello")
    await mem.persist_message(session_id, "assistant", "Hi there!")
    await mem.persist_message(session_id, "user", "Tell me about X")

    messages = await mem.load_context(session_id, max_tokens=10000)
    contents = [m["content"] for m in messages]
    assert "Hello" in contents
    assert "Hi there!" in contents
    assert "Tell me about X" in contents


@pytest.mark.asyncio
async def test_compaction(db, encryption, clean_db):
    """Old assistant messages should get breadcrumb replacements."""
    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)", session_id, "compact-session"
    )

    # Insert enough messages to trigger compaction
    for i in range(20):
        msg_type = "user" if i % 2 == 0 else "assistant"
        await mem.persist_message(session_id, msg_type, f"Message {i}", token_count=100)

    messages = await mem.load_context(session_id, max_tokens=800, always_last=5)

    # Recent messages should be intact
    recent = messages[-5:]
    assert all("[REM LOOKUP" not in m.get("content", "") for m in recent)

    # Some older assistant messages should be compacted
    # Without moments, compacted messages use "[earlier message compacted]"
    # With moments, they use "[REM LOOKUP moment-name]"
    older = messages[:-5]
    compacted = [
        m for m in older
        if "[REM LOOKUP" in m.get("content", "") or "[earlier message compacted]" in m.get("content", "")
    ]
    assert len(compacted) > 0


@pytest.mark.asyncio
async def test_moment_injection(db, encryption, clean_db):
    """Loading context should inject the latest moment summary."""
    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)", session_id, "moment-session"
    )

    # Create a moment for this session
    repo = Repository(Moment, db, encryption)
    moment = Moment(
        name="chunk-1",
        moment_type="session_chunk",
        summary="User discussed project deadlines and asked about Q4 numbers.",
        source_session_id=session_id,
    )
    await repo.upsert(moment)

    # Add some messages
    await mem.persist_message(session_id, "user", "Continue from where we left off")

    messages = await mem.load_context(session_id, max_tokens=10000)

    # First message should be the injected moment
    assert messages[0]["message_type"] == "system"
    assert "project deadlines" in messages[0]["content"]


@pytest.mark.asyncio
async def test_encrypted_messages(db, encryption, clean_db):
    """Messages with tenant_id should be encrypted at rest, decrypted on load."""
    tenant = "test-tenant-mem"
    await encryption.get_dek(tenant)

    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, tenant_id) VALUES ($1, $2, $3)",
        session_id, "enc-mem-session", tenant,
    )

    await mem.persist_message(
        session_id, "user", "My SSN is 123-45-6789", tenant_id=tenant
    )

    # Raw DB should have ciphertext
    raw = await db.fetchrow(
        "SELECT content FROM messages WHERE session_id = $1", session_id
    )
    assert raw["content"] != "My SSN is 123-45-6789"

    # Load with tenant — should decrypt
    messages = await mem.load_context(session_id, tenant_id=tenant)
    contents = [m["content"] for m in messages if m.get("message_type") == "user"]
    assert any("123-45-6789" in c for c in contents)


@pytest.mark.asyncio
async def test_token_count_auto(db, encryption, clean_db):
    """Token count auto-estimated when not provided."""
    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)", session_id, "token-session"
    )

    text = "A" * 400
    msg = await mem.persist_message(session_id, "user", text)
    assert msg.token_count == estimate_tokens(text)


@pytest.mark.asyncio
async def test_build_moment_updates_session_metadata(db, encryption, clean_db):
    """build_moment() updates session metadata with moment context."""
    mem = MemoryService(db, encryption)
    session_id = uuid4()
    await db.execute(
        "INSERT INTO sessions (id, name, metadata) VALUES ($1, $2, '{}'::jsonb)",
        session_id, "compact-meta-session",
    )

    # Add messages to create a moment from
    for i in range(6):
        msg_type = "user" if i % 2 == 0 else "assistant"
        await mem.persist_message(session_id, msg_type, f"Message {i}", token_count=100)

    moment = await mem.build_moment(session_id)

    # Verify session metadata was updated
    row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session_id)
    meta = row["metadata"]
    assert meta["latest_moment_id"] == str(moment.id)
    assert "latest_summary" in meta
    assert meta["moment_count"] == 1
