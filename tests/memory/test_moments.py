"""Integration tests for moment building — session chunks and content upload moments."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio

from p8.ontology.types import Message, Moment, Session
from p8.services.memory import MemoryService
from p8.services.repository import Repository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# NOTE: Tests omit user_id for convenience — the test database has no user
# scoping requirements. In production, moments are stamped with user_id so
# that queries like /moments/today and the CLI filter to the active user.


async def _create_session(db, encryption, *, total_tokens: int = 0) -> Session:
    """Create a session row and return it."""
    repo = Repository(Session, db, encryption)
    session = Session(name=f"test-session-{uuid4()}", mode="chat", total_tokens=total_tokens)
    [result] = await repo.upsert(session)
    return result


async def _add_messages(
    memory: MemoryService,
    session_id,
    count: int,
    *,
    token_count: int = 50,
    prefix: str = "msg",
) -> list[Message]:
    """Persist alternating user/assistant messages. Returns all persisted messages."""
    results = []
    for i in range(count):
        msg_type = "user" if i % 2 == 0 else "assistant"
        content = f"{prefix}-{i}: " + ("x" * (token_count * 4))  # ~token_count tokens
        msg = await memory.persist_message(
            session_id, msg_type, content, token_count=token_count,
        )
        results.append(msg)
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


async def test_moment_built_after_threshold(db, encryption):
    """Persist enough messages to exceed threshold → verify session_chunk moment exists."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add 6 messages × 50 tokens = 300 total tokens
    await _add_messages(memory, session.id, 6, token_count=50)

    # Threshold of 200 → should trigger
    moment = await memory.maybe_build_moment(session.id, threshold=200)
    assert moment is not None
    assert moment.moment_type == "session_chunk"
    assert moment.source_session_id == session.id
    assert moment.previous_moment_keys == []
    assert moment.starts_timestamp is not None
    assert moment.ends_timestamp is not None
    assert moment.metadata["message_count"] == 6
    assert moment.metadata["token_count"] == 300


async def test_moment_chaining(db, encryption):
    """Build two moments → verify second moment's previous_moment_keys references the first."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # First batch: 4 messages × 50 = 200 tokens
    await _add_messages(memory, session.id, 4, token_count=50)
    moment1 = await memory.maybe_build_moment(session.id, threshold=150)
    assert moment1 is not None

    # Second batch: 4 more messages × 50 = 200 more tokens
    await _add_messages(memory, session.id, 4, token_count=50, prefix="batch2")
    moment2 = await memory.maybe_build_moment(session.id, threshold=150)
    assert moment2 is not None
    assert moment2.previous_moment_keys == [moment1.name]
    assert moment2.metadata["chunk_index"] == 1


async def test_moments_injected_into_context(db, encryption):
    """Create moments for a session → load_context() → verify moment summaries as system messages."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add some messages
    await _add_messages(memory, session.id, 4, token_count=50)

    # Manually create a moment for this session
    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"session-{session.id}-chunk-0",
        moment_type="session_chunk",
        summary="Discussed project architecture and tech stack.",
        source_session_id=session.id,
        metadata={"message_count": 4, "token_count": 200, "chunk_index": 0},
    )
    await moment_repo.upsert(moment)

    # Load context — should have moment summary injected
    ctx = await memory.load_context(session.id)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1
    assert any("Discussed project architecture" in m.get("content", "") for m in system_msgs)


async def test_multiple_moments_injected(db, encryption):
    """Create 3+ moments → verify last N (not just 1) appear in context."""
    import asyncio

    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add some messages so load_context has something to return
    await _add_messages(memory, session.id, 2, token_count=10)

    # Create 4 moments with small delays so created_at ordering is deterministic
    moment_repo = Repository(Moment, db, encryption)
    for i in range(4):
        moment = Moment(
            name=f"session-{session.id}-chunk-{i}",
            moment_type="session_chunk",
            summary=f"Summary of chunk {i}.",
            source_session_id=session.id,
            metadata={"message_count": 5, "token_count": 200, "chunk_index": i},
        )
        await moment_repo.upsert(moment)
        if i < 3:
            await asyncio.sleep(0.05)

    # Load context with max_moments=3
    ctx = await memory.load_context(session.id, max_moments=3)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]

    # Should have 3 moment summaries (the most recent 3 of 4)
    moment_summaries = [m for m in system_msgs if "Summary of chunk" in m.get("content", "")]
    assert len(moment_summaries) == 3

    # The 3 most recent are chunks 1, 2, 3 — injected oldest first
    contents = " ".join(m["content"] for m in moment_summaries)
    assert "chunk 1" in contents
    assert "chunk 2" in contents
    assert "chunk 3" in contents
    # Chunk 0 (the oldest) should NOT appear since max_moments=3
    assert "chunk 0" not in contents


async def test_no_moment_below_threshold(db, encryption):
    """Small session → verify no moment created."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add 2 messages × 10 tokens = 20 total
    await _add_messages(memory, session.id, 2, token_count=10)

    moment = await memory.maybe_build_moment(session.id, threshold=200)
    assert moment is None

    # Verify no moments in DB for this session
    rows = await db.fetch(
        "SELECT * FROM moments WHERE source_session_id = $1 AND deleted_at IS NULL",
        session.id,
    )
    assert len(rows) == 0


async def test_content_upload_creates_moment(db, encryption):
    """ContentService.ingest() → verify a content_upload moment exists."""
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock, patch

    from p8.services.content import ContentService
    from p8.services.files import FileService

    @dataclass
    class _FakeChunk:
        content: str

    @dataclass
    class _FakeExtractResult:
        content: str
        chunks: list[_FakeChunk]

    settings = MagicMock()
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200

    file_service = MagicMock(spec=FileService)

    svc = ContentService(db=db, encryption=encryption, file_service=file_service, settings=settings)

    fake_result = _FakeExtractResult(
        content="Full text of the document for testing.",
        chunks=[_FakeChunk("Chunk one."), _FakeChunk("Chunk two.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        result = await svc.ingest(b"pdf bytes", "test-doc.pdf", mime_type="application/pdf")

    # Verify content_upload moment exists
    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-test-doc' AND deleted_at IS NULL"
    )
    assert len(rows) >= 1
    moment_data = dict(rows[0])
    meta = moment_data["metadata"]
    if isinstance(meta, str):
        import json
        meta = json.loads(meta)
    assert meta["source"] == "upload"
    assert meta["chunk_count"] == 2
    assert "file_id" in meta


async def test_content_upload_moment_in_feed(db, encryption):
    """Ingest content → verify the upload moment can be found in the moments table."""
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock, patch

    from p8.services.content import ContentService
    from p8.services.files import FileService

    @dataclass
    class _FakeChunk:
        content: str

    @dataclass
    class _FakeExtractResult:
        content: str
        chunks: list[_FakeChunk]

    settings = MagicMock()
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200

    file_service = MagicMock(spec=FileService)

    svc = ContentService(db=db, encryption=encryption, file_service=file_service, settings=settings)

    fake_result = _FakeExtractResult(
        content="Report about Q4 financials.",
        chunks=[_FakeChunk("Q4 revenue was up 20%.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        await svc.ingest(b"pdf bytes", "q4-report.pdf", mime_type="application/pdf")

    # Query moments table directly — simulates what load_context would find
    moment_repo = Repository(Moment, db, encryption)
    moments = await moment_repo.find(filters={"moment_type": "content_upload"})
    assert len(moments) >= 1
    m = moments[0]
    assert m.name == "upload-q4-report"
    assert "q4-report.pdf" in (m.summary or "")
    assert m.metadata["resource_keys"] == ["q4-report-chunk-0000"]


# ---------------------------------------------------------------------------
# Session timeline tests
# ---------------------------------------------------------------------------


async def test_session_timeline_interleaves_messages_and_moments(db, encryption):
    """Messages + moment in a session → timeline returns both types chronologically."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add 6 messages
    await _add_messages(memory, session.id, 6, token_count=50)

    # Create a moment for this session
    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"session-{session.id}-chunk-0",
        moment_type="session_chunk",
        summary="Discussed architecture.",
        source_session_id=session.id,
        metadata={"message_count": 6, "token_count": 300, "chunk_index": 0},
    )
    await moment_repo.upsert(moment)

    # Get timeline
    timeline = await db.rem_session_timeline(session.id)
    assert len(timeline) == 7  # 6 messages + 1 moment

    event_types = {r["event_type"] for r in timeline}
    assert "message" in event_types
    assert "moment" in event_types

    # Verify chronological order
    timestamps = [r["event_timestamp"] for r in timeline]
    assert timestamps == sorted(timestamps)


async def test_session_timeline_empty_session(db, encryption):
    """Empty session → timeline returns empty list."""
    session = await _create_session(db, encryption)
    timeline = await db.rem_session_timeline(session.id)
    assert timeline == []


# ---------------------------------------------------------------------------
# Today summary tests
# ---------------------------------------------------------------------------


async def test_today_summary_with_activity(db, encryption):
    """Messages today → build_today_summary() returns valid summary."""
    from uuid import UUID
    test_uid = UUID("aaaaaaaa-0000-0000-0000-000000000001")
    # Clean prior run's messages for this user so count is deterministic
    await db.execute("DELETE FROM messages WHERE user_id = $1", test_uid)
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)
    # Stamp session with test user
    await db.execute("UPDATE sessions SET user_id = $1 WHERE id = $2", test_uid, session.id)

    await _add_messages(memory, session.id, 4, token_count=25)
    # Stamp messages with test user
    await db.execute("UPDATE messages SET user_id = $1 WHERE session_id = $2", test_uid, session.id)

    today = await memory.build_today_summary(user_id=test_uid)
    assert today is not None
    assert today["moment_type"] == "today_summary"
    assert today["metadata"]["message_count"] == 4
    assert today["metadata"]["total_tokens"] == 100
    assert len(today["metadata"]["sessions"]) >= 1


async def test_today_summary_no_activity(db, encryption):
    """No messages today → build_today_summary() returns None."""
    from uuid import UUID
    # Use a user_id that definitely has no messages
    nobody_uid = UUID("aaaaaaaa-0000-0000-0000-ffffffffffff")
    memory = MemoryService(db, encryption)
    today = await memory.build_today_summary(user_id=nobody_uid)
    assert today is None
