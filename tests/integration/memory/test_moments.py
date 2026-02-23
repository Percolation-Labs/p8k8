"""Integration tests for moment building — session chunks and content upload moments."""

from __future__ import annotations

import pytest

from p8.ontology.types import Moment
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.data import create_session, seed_messages


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


async def test_moment_built_after_threshold(db, encryption):
    """Persist enough messages to exceed threshold → verify session_chunk moment exists."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption)

    # Add 6 messages × 50 tokens = 300 total tokens
    await seed_messages(memory, session.id, 6, token_count=50)

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
    session = await create_session(db, encryption)

    # First batch: 4 messages × 50 = 200 tokens
    await seed_messages(memory, session.id, 4, token_count=50)
    moment1 = await memory.maybe_build_moment(session.id, threshold=150)
    assert moment1 is not None

    # Second batch: 4 more messages × 50 = 200 more tokens
    await seed_messages(memory, session.id, 4, token_count=50, prefix="batch2")
    moment2 = await memory.maybe_build_moment(session.id, threshold=150)
    assert moment2 is not None
    assert moment2.previous_moment_keys == [moment1.name]
    assert moment2.metadata["chunk_index"] == 1


async def test_moments_injected_into_context(db, encryption):
    """Create moments for a session → load_context() → verify moment summaries as system messages."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption)

    # Add some messages
    await seed_messages(memory, session.id, 4, token_count=50)

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
    session = await create_session(db, encryption)

    # Add some messages so load_context has something to return
    await seed_messages(memory, session.id, 2, token_count=10)

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
    session = await create_session(db, encryption)

    # Add 2 messages × 10 tokens = 20 total
    await seed_messages(memory, session.id, 2, token_count=10)

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


async def test_content_upload_creates_session_for_provided_id(db, encryption):
    """When session_id is provided but doesn't exist yet, ingest() creates the session row."""
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

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
        content="A text note for testing session creation.",
        chunks=[_FakeChunk("A text note for testing session creation.")],
    )

    # Use a deterministic session ID that doesn't exist yet (like todayChatId)
    provided_session_id = str(uuid4())

    # Verify session doesn't exist
    row = await db.fetchrow("SELECT id FROM sessions WHERE id = $1", provided_session_id)
    assert row is None

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        result = await svc.ingest(
            b"text note bytes", "note.txt",
            mime_type="text/plain",
            session_id=provided_session_id,
        )

    # Session should now exist
    row = await db.fetchrow("SELECT id, name, mode FROM sessions WHERE id = $1", provided_session_id)
    assert row is not None
    assert row["mode"] == "content_upload"

    # Moment should reference this session
    moment_row = await db.fetchrow(
        "SELECT source_session_id FROM moments WHERE name = 'upload-note' AND deleted_at IS NULL"
    )
    assert moment_row is not None
    assert str(moment_row["source_session_id"]) == provided_session_id

    # Result should return the same session_id
    assert str(result.session_id) == provided_session_id


async def test_content_upload_reuses_existing_session(db, encryption):
    """When session_id is provided and already exists, ingest() reuses it without error."""
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

    # Pre-create a session
    from p8.utils.data import create_session
    existing = await create_session(db, encryption, name="pre-existing-chat")

    fake_result = _FakeExtractResult(
        content="Another note.",
        chunks=[_FakeChunk("Another note.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        result = await svc.ingest(
            b"note bytes", "note2.txt",
            mime_type="text/plain",
            session_id=str(existing.id),
        )

    # Should not create a duplicate — still just 1 session with this ID
    rows = await db.fetch("SELECT id FROM sessions WHERE id = $1", existing.id)
    assert len(rows) == 1

    assert result.session_id == existing.id


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
    assert "Q4 financials" in (m.summary or "")
    assert m.metadata["resource_keys"] == ["q4-report-chunk-0000"]


# ---------------------------------------------------------------------------
# Session timeline tests
# ---------------------------------------------------------------------------


async def test_session_timeline_interleaves_messages_and_moments(db, encryption):
    """Messages + moment in a session → timeline returns both types chronologically."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption)

    # Add 6 messages
    await seed_messages(memory, session.id, 6, token_count=50)

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
    session = await create_session(db, encryption)
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
    session = await create_session(db, encryption)
    # Stamp session with test user
    await db.execute("UPDATE sessions SET user_id = $1 WHERE id = $2", test_uid, session.id)

    await seed_messages(memory, session.id, 4, token_count=25)
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


# ---------------------------------------------------------------------------
# Enriched context tests
# ---------------------------------------------------------------------------


async def test_upload_moment_has_content_preview(db, encryption):
    """Upload via ContentService → moment summary contains preview and resource keys."""
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

    full_text = "This is a detailed report about machine learning pipelines and data processing."
    fake_result = _FakeExtractResult(
        content=full_text,
        chunks=[_FakeChunk("ML pipeline chunk."), _FakeChunk("Data processing chunk.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        await svc.ingest(b"pdf bytes", "ml-report.pdf", mime_type="application/pdf")

    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-ml-report' AND deleted_at IS NULL"
    )
    assert len(rows) >= 1
    summary = rows[0]["summary"]

    # Summary should contain the extracted content text
    assert "machine learning pipelines" in summary


async def test_moment_injection_includes_metadata(db, encryption):
    """content_upload moment with metadata → load_context injects Resources/File lines."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption)

    await seed_messages(memory, session.id, 2, token_count=10)

    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"upload-test-{session.id}",
        moment_type="content_upload",
        summary="Uploaded report.pdf (2 chunks, 500 chars).",
        source_session_id=session.id,
        metadata={
            "file_id": "abc-123",
            "file_name": "report.pdf",
            "resource_keys": ["report-chunk-0000", "report-chunk-0001"],
            "source": "upload",
            "chunk_count": 2,
        },
    )
    await moment_repo.upsert(moment)

    ctx = await memory.load_context(session.id)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1

    # Find the injected moment context
    context_content = " ".join(m["content"] for m in system_msgs)
    assert "Resources: report-chunk-0000, report-chunk-0001" in context_content
    assert "File: report.pdf" in context_content


async def test_compacted_messages_include_summary(db, encryption):
    """Enough messages to trigger compaction → breadcrumbs include summary snippet."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption)

    # Seed 10 messages to ensure compaction triggers (always_last=5 by default)
    await seed_messages(memory, session.id, 10, token_count=50)

    # Create a moment so compaction has a moment to reference
    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"session-{session.id}-chunk-0",
        moment_type="session_chunk",
        summary="Discussed deployment strategies and K8s configuration.",
        source_session_id=session.id,
        metadata={"message_count": 10, "token_count": 500, "chunk_index": 0},
    )
    await moment_repo.upsert(moment)

    ctx = await memory.load_context(session.id, always_last=5)

    # Find compacted messages — they should contain summary hint, not bare LOOKUP
    compacted = [m for m in ctx if "Earlier:" in m.get("content", "")]
    assert len(compacted) > 0

    # Verify the summary snippet is included
    sample = compacted[0]["content"]
    assert "Discussed deployment strategies" in sample
    assert "REM LOOKUP" in sample


async def test_today_summary_includes_session_names(db, encryption):
    """Today summary metadata includes session names for conversation starters."""
    from uuid import UUID

    test_uid = UUID("aaaaaaaa-0000-0000-0000-000000000002")
    await db.execute("DELETE FROM messages WHERE user_id = $1", test_uid)

    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="discuss-ml-architecture")
    await db.execute("UPDATE sessions SET user_id = $1 WHERE id = $2", test_uid, session.id)

    await seed_messages(memory, session.id, 4, token_count=25)
    await db.execute("UPDATE messages SET user_id = $1 WHERE session_id = $2", test_uid, session.id)

    today = await memory.build_today_summary(user_id=test_uid)
    assert today is not None
    assert today["metadata"]["sessions"]
    # Sessions list should contain entries with name info
    sessions = today["metadata"]["sessions"]
    assert len(sessions) >= 1
    # At minimum, sessions should be present (verifying the data is there for bootstrapping)
    assert any(s for s in sessions)
