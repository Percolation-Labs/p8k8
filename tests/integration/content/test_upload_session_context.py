"""End-to-end integration tests for content upload → session → agent context.

Proves that:
1. Content uploads create moments with correct metadata
2. Sessions are created/enriched with upload context
3. The agent sees upload context in both session metadata and moment injection
4. Multiple uploads accumulate in session metadata
5. Pre-existing sessions get updated (not just new ones)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from p8.ontology.types import Moment, Session
from p8.services.content import ContentService
from p8.services.files import FileService
from p8.services.memory import MemoryService, format_moment_context
from p8.services.repository import Repository
from p8.utils.data import create_session, seed_messages


USER_ID = UUID("aaaaaaaa-0000-0000-0000-222222222222")


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


def _make_content_service(db, encryption):
    settings = MagicMock()
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200
    file_service = MagicMock(spec=FileService)
    return ContentService(db=db, encryption=encryption, file_service=file_service, settings=settings)


@dataclass
class _FakeChunk:
    content: str


@dataclass
class _FakeExtractResult:
    content: str
    chunks: list[_FakeChunk]


def _patch_kreuzberg(full_text: str, chunks: list[str] | None = None):
    """Context manager that patches kreuzberg to return given text."""
    if chunks is None:
        chunks = [full_text]
    fake = _FakeExtractResult(content=full_text, chunks=[_FakeChunk(c) for c in chunks])
    return (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    )


# ---------------------------------------------------------------------------
# 1. Upload creates moment with correct metadata
# ---------------------------------------------------------------------------


async def test_upload_creates_moment_with_metadata(db, encryption):
    """File upload → moment has file_name, resource_keys, source in metadata."""
    svc = _make_content_service(db, encryption)
    p1, p2, p3 = _patch_kreuzberg("Meeting notes about Q1 planning.", ["Meeting notes about Q1 planning."])

    with p1, p2, p3:
        result = await svc.ingest(b"text content", "q1-notes.txt", mime_type="text/plain")

    # Check moment
    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-q1-notes' AND deleted_at IS NULL"
    )
    assert len(rows) == 1
    moment = dict(rows[0])
    meta = moment["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert meta["file_name"] == "q1-notes.txt"
    assert meta["source"] == "upload"
    assert meta["chunk_count"] == 1
    assert "q1-notes-chunk-0000" in meta["resource_keys"]
    assert moment["moment_type"] == "content_upload"
    assert "Meeting notes" in moment["summary"]


# ---------------------------------------------------------------------------
# 2. Upload without session_id creates a new session with metadata
# ---------------------------------------------------------------------------


async def test_upload_without_session_creates_session(db, encryption):
    """Upload with no session_id → new session created with upload metadata."""
    svc = _make_content_service(db, encryption)
    p1, p2, p3 = _patch_kreuzberg("Project roadmap for H2.")

    with p1, p2, p3:
        result = await svc.ingest(
            b"roadmap", "roadmap.txt", mime_type="text/plain", user_id=USER_ID,
        )

    assert result.session_id is not None

    # Session should exist with upload metadata
    session_row = await db.fetchrow("SELECT * FROM sessions WHERE id = $1", result.session_id)
    assert session_row is not None
    assert session_row["mode"] == "content_upload"

    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert "roadmap-chunk-0000" in meta["resource_keys"]
    assert len(meta["uploads"]) == 1
    assert meta["uploads"][0]["file_name"] == "roadmap.txt"
    assert meta["uploads"][0]["source"] == "upload"

    # Moment should be linked to this session
    moment_row = await db.fetchrow(
        "SELECT source_session_id FROM moments WHERE name = 'upload-roadmap' AND deleted_at IS NULL"
    )
    assert str(moment_row["source_session_id"]) == str(result.session_id)


# ---------------------------------------------------------------------------
# 3. Upload with existing session_id enriches session metadata
# ---------------------------------------------------------------------------


async def test_upload_enriches_existing_session_metadata(db, encryption):
    """Upload to a pre-existing chat session → session metadata gets upload context."""
    svc = _make_content_service(db, encryption)

    # Create a chat session first (simulating what ChatController does)
    existing = await create_session(db, encryption, name="Today", user_id=USER_ID)
    assert existing.metadata == {}

    p1, p2, p3 = _patch_kreuzberg("Architecture design document.")

    with p1, p2, p3:
        result = await svc.ingest(
            b"arch doc", "architecture.txt", mime_type="text/plain",
            session_id=str(existing.id), user_id=USER_ID,
        )

    assert result.session_id == existing.id

    # Session metadata should now contain upload info
    session_row = await db.fetchrow("SELECT metadata, name FROM sessions WHERE id = $1", existing.id)
    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert "uploads" in meta
    assert len(meta["uploads"]) == 1
    assert meta["uploads"][0]["file_name"] == "architecture.txt"
    assert meta["uploads"][0]["source"] == "upload"
    assert "architecture-chunk-0000" in meta["resource_keys"]

    # Original session name should be preserved
    assert session_row["name"] == "Today"


# ---------------------------------------------------------------------------
# 4. Multiple uploads accumulate in session metadata
# ---------------------------------------------------------------------------


async def test_multiple_uploads_accumulate_metadata(db, encryption):
    """Two uploads to same session → both appear in metadata."""
    svc = _make_content_service(db, encryption)
    session = await create_session(db, encryption, name="Today", user_id=USER_ID)

    # First upload
    p1, p2, p3 = _patch_kreuzberg("First document content.")
    with p1, p2, p3:
        await svc.ingest(
            b"doc1", "doc1.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    # Second upload
    p1, p2, p3 = _patch_kreuzberg("Second document content.")
    with p1, p2, p3:
        await svc.ingest(
            b"doc2", "doc2.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session.id)
    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert len(meta["uploads"]) == 2
    filenames = [u["file_name"] for u in meta["uploads"]]
    assert "doc1.txt" in filenames
    assert "doc2.txt" in filenames

    # resource_keys should have both
    assert "doc1-chunk-0000" in meta["resource_keys"]
    assert "doc2-chunk-0000" in meta["resource_keys"]


# ---------------------------------------------------------------------------
# 5. Agent sees upload moment in load_context
# ---------------------------------------------------------------------------


async def test_agent_sees_upload_moment_in_context(db, encryption):
    """Upload moment with source_session_id → load_context injects it as system message."""
    svc = _make_content_service(db, encryption)
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Today", user_id=USER_ID)

    # Upload a file to the session
    p1, p2, p3 = _patch_kreuzberg("Budget report for Q4 with revenue projections.", ["Budget report for Q4 with revenue projections."])
    with p1, p2, p3:
        await svc.ingest(
            b"budget data", "budget-q4.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    # Add a user message so load_context has something to return
    await seed_messages(memory, session.id, 2, token_count=10, user_id=USER_ID)

    # Load context as the agent would
    ctx = await memory.load_context(session.id)

    # Find injected moment context
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1

    # The upload moment summary should be present
    all_system_content = " ".join(m.get("content", "") for m in system_msgs)
    assert "budget-q4.txt" in all_system_content or "Budget report" in all_system_content
    assert "Resources:" in all_system_content
    assert "budget-q4-chunk-0000" in all_system_content


# ---------------------------------------------------------------------------
# 6. Agent sees session metadata in ContextInjector instructions
# ---------------------------------------------------------------------------


async def test_agent_instructions_include_session_metadata(db, encryption):
    """Session with upload metadata → ContextInjector.instructions includes it."""
    from p8.agentic.types import ContextAttributes

    svc = _make_content_service(db, encryption)
    session = await create_session(db, encryption, name="Today", user_id=USER_ID)

    p1, p2, p3 = _patch_kreuzberg("Design spec for the new API.")
    with p1, p2, p3:
        await svc.ingest(
            b"spec", "api-spec.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    # Reload session to get updated metadata
    repo = Repository(Session, db, encryption)
    session = await repo.get(session.id)

    # Build context attributes as ChatController.prepare() would
    attrs = ContextAttributes(
        user_id=USER_ID,
        session_id=str(session.id),
        session_name=session.name,
        session_metadata=session.metadata,
    )
    rendered = attrs.render()

    # Session Context section should include upload metadata
    assert "## Session Context" in rendered
    assert "api-spec" in rendered
    assert "resource_keys" in rendered
    assert "uploads" in rendered


# ---------------------------------------------------------------------------
# 7. Full end-to-end: upload → chat → agent has full context
# ---------------------------------------------------------------------------


async def test_end_to_end_upload_then_agent_context(db, encryption):
    """Full flow: create session → upload file → seed messages → verify agent sees everything."""
    from p8.agentic.types import ContextAttributes

    svc = _make_content_service(db, encryption)
    memory = MemoryService(db, encryption)

    # Step 1: Create a chat session (like the Flutter app does)
    session = await create_session(db, encryption, name="Today", user_id=USER_ID)
    assert session.metadata == {}

    # Step 2: Upload a text note to the session
    p1, p2, p3 = _patch_kreuzberg(
        "Meeting with Sarah: discussed ML pipeline refactoring and deployment timeline.",
        ["Meeting with Sarah: discussed ML pipeline refactoring and deployment timeline."],
    )
    with p1, p2, p3:
        result = await svc.ingest(
            b"meeting notes", "sarah-meeting.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )
    assert result.session_id == session.id

    # Step 3: Add some chat messages (user asks about the upload)
    await seed_messages(memory, session.id, 2, token_count=10, prefix="chat", user_id=USER_ID)

    # Step 4: Verify session metadata was enriched
    repo = Repository(Session, db, encryption)
    session = await repo.get(session.id)
    assert session.metadata.get("uploads") is not None
    assert len(session.metadata["uploads"]) == 1
    assert "sarah-meeting-chunk-0000" in session.metadata["resource_keys"]

    # Step 5: Verify moment is linked to session
    moment_rows = await db.fetch(
        "SELECT * FROM moments WHERE source_session_id = $1 AND deleted_at IS NULL",
        session.id,
    )
    upload_moments = [dict(r) for r in moment_rows if r["moment_type"] == "content_upload"]
    assert len(upload_moments) == 1
    assert "sarah-meeting" in upload_moments[0]["name"]

    # Step 6: Verify load_context returns moment as system context
    ctx = await memory.load_context(session.id)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1
    system_content = " ".join(m.get("content", "") for m in system_msgs)
    assert "sarah-meeting" in system_content

    # Step 7: Verify ContextInjector instructions include session metadata
    attrs = ContextAttributes(
        user_id=USER_ID,
        session_id=str(session.id),
        session_name=session.name,
        session_metadata=session.metadata,
    )
    instructions = attrs.render()
    assert "## Session Context" in instructions
    assert "sarah-meeting" in instructions
    assert "resource_keys" in instructions

    # Step 8: Verify the agent would see BOTH:
    # a) Session metadata in instructions (session-level context)
    # b) Moment summary in message history (moment injection)
    # These are complementary — metadata tells the agent what files exist,
    # moment injection gives the content preview and resource keys.
    user_msgs = [m for m in ctx if m.get("message_type") == "user"]
    assert len(user_msgs) >= 1  # chat messages present


# ---------------------------------------------------------------------------
# 8. format_moment_context includes upload metadata
# ---------------------------------------------------------------------------


async def test_format_moment_context_includes_upload_fields(db, encryption):
    """format_moment_context renders resource keys and file name from moment metadata."""
    moment_dict = {
        "summary": "Uploaded report.pdf (3 chunks, 2500 chars).",
        "metadata": {
            "file_id": "abc-123",
            "file_name": "report.pdf",
            "resource_keys": ["report-chunk-0000", "report-chunk-0001", "report-chunk-0002"],
            "source": "upload",
            "chunk_count": 3,
        },
        "topic_tags": ["finance", "quarterly"],
    }
    rendered = format_moment_context(moment_dict)

    assert "[Session context]" in rendered
    assert "report.pdf" in rendered
    assert "report-chunk-0000" in rendered
    assert "Topics: finance, quarterly" in rendered


# ---------------------------------------------------------------------------
# 9. Compaction: build_moment stamps session metadata + agent sees it
# ---------------------------------------------------------------------------


async def test_compaction_updates_session_metadata(db, encryption):
    """build_moment → session metadata gets latest_moment_id and latest_summary."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Chat", user_id=USER_ID)

    # Seed enough messages to trigger compaction
    await seed_messages(memory, session.id, 6, token_count=100, user_id=USER_ID)

    # Build a moment (threshold=0 → always builds)
    moment = await memory.build_moment(session.id)
    assert moment is not None

    # Session metadata should now contain compaction info
    row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session.id)
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert "latest_moment_id" in meta
    assert meta["latest_moment_id"] == str(moment.id)
    assert "latest_summary" in meta
    assert "moment_count" in meta
    assert meta["moment_count"] >= 1


async def test_compaction_moment_injected_in_context(db, encryption):
    """After compaction, load_context injects the session_chunk moment as system message."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Chat", user_id=USER_ID)

    # Seed messages and build a moment
    await seed_messages(memory, session.id, 6, token_count=100, prefix="conv", user_id=USER_ID)
    moment = await memory.build_moment(session.id)
    assert moment is not None

    # Add a couple more messages after the moment
    await seed_messages(memory, session.id, 2, token_count=10, prefix="after", user_id=USER_ID)

    # Load context — should include the moment as a system message
    ctx = await memory.load_context(session.id)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1

    system_content = " ".join(m.get("content", "") for m in system_msgs)
    assert "[Session context]" in system_content


async def test_compaction_agent_instructions_include_session_metadata(db, encryption):
    """After compaction, ContextAttributes.render() includes latest_summary in instructions."""
    from p8.agentic.types import ContextAttributes

    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Chat", user_id=USER_ID)

    await seed_messages(memory, session.id, 6, token_count=100, prefix="talk", user_id=USER_ID)
    moment = await memory.build_moment(session.id)
    assert moment is not None

    # Reload session to get updated metadata
    repo = Repository(Session, db, encryption)
    session = await repo.get(session.id)

    attrs = ContextAttributes(
        user_id=USER_ID,
        session_id=str(session.id),
        session_name=session.name,
        session_metadata=session.metadata,
    )
    rendered = attrs.render()

    assert "## Session Context" in rendered
    assert "latest_moment_id" in rendered
    assert "latest_summary" in rendered


# ---------------------------------------------------------------------------
# 12. Feed returns session data alongside moments (LEFT JOIN)
# ---------------------------------------------------------------------------


async def test_feed_returns_session_data_with_moments(db, encryption):
    """rem_moments_feed includes session_name/session_metadata for upload moments."""
    svc = _make_content_service(db, encryption)
    session = await create_session(db, encryption, name="Research", user_id=USER_ID)

    p1, p2, p3 = _patch_kreuzberg("Analysis of market trends.")
    with p1, p2, p3:
        await svc.ingest(
            b"data", "trends.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    # Query the feed
    feed = await db.rem_moments_feed(user_id=USER_ID, limit=5)
    upload_items = [f for f in feed if f.get("moment_type") == "content_upload"]
    assert len(upload_items) >= 1

    item = upload_items[0]
    meta = item["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    # Session data should be present via LEFT JOIN
    assert meta.get("session_name") is not None
    assert meta.get("session_metadata") is not None
    session_meta = meta["session_metadata"]
    if isinstance(session_meta, str):
        session_meta = json.loads(session_meta)
    assert "uploads" in session_meta
    assert "resource_keys" in session_meta


async def test_feed_handles_moments_without_session(db, encryption):
    """Moments without source_session_id still appear in feed (LEFT JOIN = NULLs)."""
    # Create a standalone moment with no session
    repo = Repository(Moment, db, encryption)
    moment = Moment(
        name="standalone-note",
        moment_type="dream",
        summary="A standalone insight.",
        user_id=USER_ID,
    )
    await repo.upsert(moment)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=5)
    dream_items = [f for f in feed if f.get("name") == "standalone-note"]
    assert len(dream_items) == 1

    meta = dream_items[0]["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    # session fields should be null (LEFT JOIN)
    assert meta.get("session_name") is None


# ---------------------------------------------------------------------------
# 14. Message compaction — old assistant messages become breadcrumbs
# ---------------------------------------------------------------------------


async def test_compaction_breadcrumbs_in_old_messages(db, encryption):
    """After compaction, old assistant messages outside the recent window become LOOKUP breadcrumbs."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Deep Chat", user_id=USER_ID)

    # Seed enough messages: 10 messages with alternating user/assistant
    for i in range(10):
        msg_type = "user" if i % 2 == 0 else "assistant"
        await memory.persist_message(
            session.id, msg_type, f"Message {i} content about project planning.",
            token_count=100, user_id=USER_ID,
        )

    # Build a moment to cover these messages
    moment = await memory.build_moment(session.id)
    assert moment is not None

    # Add recent messages after the moment
    for i in range(6):
        msg_type = "user" if i % 2 == 0 else "assistant"
        await memory.persist_message(
            session.id, msg_type, f"Recent message {i}.",
            token_count=10, user_id=USER_ID,
        )

    # Load context with default always_last=5
    ctx = await memory.load_context(session.id, always_last=5)

    # Find compacted breadcrumb messages
    breadcrumbs = [
        m for m in ctx
        if m.get("message_type") == "assistant"
        and m.get("content", "").startswith("[Earlier:")
    ]
    assert len(breadcrumbs) >= 1, "Expected at least one compacted breadcrumb"

    # Breadcrumb should contain the moment name for LOOKUP
    bc = breadcrumbs[0]["content"]
    assert "REM LOOKUP" in bc
    assert moment.name in bc


async def test_resource_chunk_lookup_after_upload(db, encryption):
    """After file upload, REM LOOKUP returns full entity data for resource chunks."""
    svc = _make_content_service(db, encryption)

    chunks = [
        "Chapter 1: Climate models show warming acceleration.",
        "Chapter 2: Sea level projections through 2100.",
    ]
    p1, p2, p3 = _patch_kreuzberg(" ".join(chunks), chunks)
    with p1, p2, p3:
        result = await svc.ingest(
            b"climate data", "climate-report.pdf", mime_type="application/pdf", user_id=USER_ID,
        )

    assert result.chunk_count == 2

    # REM LOOKUP should return the full resource entity row (not just kv_store summary)
    results = await db.rem_lookup("climate-report-chunk-0000")
    assert len(results) == 1
    assert results[0]["entity_type"] == "resources"

    data = results[0]["data"]
    # Full entity fields present
    assert data["name"] == "climate-report-chunk-0000"
    assert data["content"] == chunks[0]  # actual text content, not just the key name
    assert data["ordinal"] == 0
    assert data["metadata"]["source_filename"] == "climate-report.pdf"
    assert data["graph_edges"][0]["target"] == "climate-report"
    assert data["graph_edges"][0]["relation"] == "chunk_of"

    # Second chunk also resolvable
    results2 = await db.rem_lookup("climate-report-chunk-0001")
    assert len(results2) == 1
    assert results2[0]["data"]["content"] == chunks[1]
    assert results2[0]["data"]["ordinal"] == 1

    # LOOKUP the upload moment — should also return full entity
    results_m = await db.rem_lookup("upload-climate-report")
    assert len(results_m) == 1
    assert results_m[0]["entity_type"] == "moments"
    m_data = results_m[0]["data"]
    assert m_data["moment_type"] == "content_upload"
    assert m_data["metadata"]["file_name"] == "climate-report.pdf"
    assert "climate-report-chunk-0000" in m_data["metadata"]["resource_keys"]
    assert "climate-report-chunk-0001" in m_data["metadata"]["resource_keys"]


# ---------------------------------------------------------------------------
# 16. Audio upload creates moment + session with correct metadata
# ---------------------------------------------------------------------------


async def test_audio_upload_creates_moment_and_session(db, encryption):
    """Audio upload → transcription → moment with file_name, session with uploads[]."""
    svc = _make_content_service(db, encryption)
    svc.settings.openai_api_key = "test-key"
    svc.settings.audio_chunk_duration_ms = 30000
    svc.settings.audio_silence_thresh = -40
    svc.settings.audio_min_silence_len = 700

    mock_segment = MagicMock()
    mock_segment.export = MagicMock(side_effect=lambda buf, format: buf.write(b"fake-wav"))
    mock_segment.__len__ = MagicMock(return_value=5000)

    mock_audio = MagicMock()

    mock_response = MagicMock()
    mock_response.text = "Discussed quarterly goals and hiring plan."
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    rechunk = _FakeExtractResult(
        content="Discussed quarterly goals and hiring plan.",
        chunks=[_FakeChunk("Discussed quarterly goals and hiring plan.")],
    )

    with (
        patch("pydub.AudioSegment.from_file", return_value=mock_audio),
        patch("pydub.silence.split_on_silence", return_value=[mock_segment]),
        patch("pydub.utils.make_chunks", return_value=[mock_segment]),
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=rechunk),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        result = await svc.ingest(
            b"audio-data", "meeting.m4a", mime_type="audio/mp4", user_id=USER_ID,
        )

    assert result.session_id is not None
    assert result.chunk_count == 1

    # Moment created with correct metadata
    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-meeting' AND deleted_at IS NULL"
    )
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["file_name"] == "meeting.m4a"
    assert meta["source"] == "upload"
    assert "meeting-chunk-0000" in meta["resource_keys"]

    # Session has upload metadata
    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", result.session_id)
    smeta = session_row["metadata"]
    if isinstance(smeta, str):
        smeta = json.loads(smeta)
    assert len(smeta["uploads"]) == 1
    assert smeta["uploads"][0]["file_name"] == "meeting.m4a"


# ---------------------------------------------------------------------------
# 17. Image upload with thumbnail → moment has image_uri
# ---------------------------------------------------------------------------


async def test_image_upload_creates_moment_with_image_uri(db, encryption):
    """Image upload → moment has image_uri (base64 thumbnail), session has metadata."""
    svc = _make_content_service(db, encryption)

    # Create a tiny valid JPEG (1x1 pixel)
    from PIL import Image
    buf = __import__("io").BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, format="JPEG")
    image_bytes = buf.getvalue()

    result = await svc.ingest(
        image_bytes, "photo.jpg", mime_type="image/jpeg", user_id=USER_ID,
    )

    assert result.session_id is not None
    assert result.chunk_count == 0  # images have no text chunks

    # Moment should have image_uri
    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-photo' AND deleted_at IS NULL"
    )
    assert len(rows) == 1
    assert rows[0]["image_uri"] is not None
    assert rows[0]["image_uri"].startswith("data:image/jpeg;base64,")

    # Session has upload metadata
    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", result.session_id)
    smeta = session_row["metadata"]
    if isinstance(smeta, str):
        smeta = json.loads(smeta)
    assert smeta["uploads"][0]["file_name"] == "photo.jpg"


# ---------------------------------------------------------------------------
# 18. Upload to session that has prior compaction moments
# ---------------------------------------------------------------------------


async def test_upload_to_session_with_prior_compaction(db, encryption):
    """Upload file to a chat session that already has session_chunk moments."""
    memory = MemoryService(db, encryption)
    svc = _make_content_service(db, encryption)

    # Create session, seed messages, build a compaction moment
    session = await create_session(db, encryption, name="Project Chat", user_id=USER_ID)
    await seed_messages(memory, session.id, 6, token_count=100, prefix="chat", user_id=USER_ID)
    moment = await memory.build_moment(session.id)
    assert moment is not None
    assert moment.moment_type == "session_chunk"

    # Now upload a file to the same session
    p1, p2, p3 = _patch_kreuzberg("Sprint retrospective notes.")
    with p1, p2, p3:
        result = await svc.ingest(
            b"retro notes", "retro.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    assert result.session_id == session.id

    # Session metadata should have BOTH compaction and upload info
    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session.id)
    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    # Compaction stamps these
    assert "latest_moment_id" in meta
    assert "moment_count" in meta
    # Upload stamps these
    assert "uploads" in meta
    assert len(meta["uploads"]) == 1
    assert "retro-chunk-0000" in meta["resource_keys"]

    # Both moment types should exist for this session
    all_moments = await db.fetch(
        "SELECT moment_type FROM moments WHERE source_session_id = $1 AND deleted_at IS NULL",
        session.id,
    )
    types = {r["moment_type"] for r in all_moments}
    assert "session_chunk" in types
    assert "content_upload" in types


# ---------------------------------------------------------------------------
# 19. Multiple compaction moments chain correctly
# ---------------------------------------------------------------------------


async def test_multiple_compaction_moments_chain(db, encryption):
    """Two sequential build_moment calls → second has previous_moment_keys pointing to first."""
    memory = MemoryService(db, encryption)
    session = await create_session(db, encryption, name="Long Chat", user_id=USER_ID)

    # First batch of messages → first moment
    await seed_messages(memory, session.id, 6, token_count=100, prefix="batch1", user_id=USER_ID)
    m1 = await memory.build_moment(session.id)
    assert m1 is not None

    # Second batch → second moment
    await seed_messages(memory, session.id, 6, token_count=100, prefix="batch2", user_id=USER_ID)
    m2 = await memory.build_moment(session.id)
    assert m2 is not None

    # Chaining: m2 references m1
    assert m2.previous_moment_keys == [m1.name]

    # Session metadata updated with latest
    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session.id)
    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["latest_moment_id"] == str(m2.id)
    assert meta["moment_count"] == 2


# ---------------------------------------------------------------------------
# 20. Mixed: upload then compaction on same session
# ---------------------------------------------------------------------------


async def test_upload_then_compaction_on_same_session(db, encryption):
    """Upload a file, then chat and compact — both moment types coexist with correct metadata."""
    memory = MemoryService(db, encryption)
    svc = _make_content_service(db, encryption)

    session = await create_session(db, encryption, name="Mixed Session", user_id=USER_ID)

    # Step 1: Upload a file
    p1, p2, p3 = _patch_kreuzberg("Design spec for the new API.")
    with p1, p2, p3:
        upload_result = await svc.ingest(
            b"spec", "api-spec.txt", mime_type="text/plain",
            session_id=str(session.id), user_id=USER_ID,
        )

    # Step 2: Chat about the upload, then compact
    await seed_messages(memory, session.id, 8, token_count=100, prefix="discuss", user_id=USER_ID)
    chunk_moment = await memory.build_moment(session.id)
    assert chunk_moment is not None

    # Session metadata should have both upload info and compaction info
    session_row = await db.fetchrow("SELECT metadata FROM sessions WHERE id = $1", session.id)
    meta = session_row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)

    assert "uploads" in meta
    assert meta["uploads"][0]["file_name"] == "api-spec.txt"
    assert "api-spec-chunk-0000" in meta["resource_keys"]
    assert meta["latest_moment_id"] == str(chunk_moment.id)
    assert meta["moment_count"] >= 1

    # Agent should see both context sources on reload
    ctx = await memory.load_context(session.id)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1  # at least the upload moment

    # ContextAttributes should include everything
    from p8.agentic.types import ContextAttributes
    repo = Repository(Session, db, encryption)
    session = await repo.get(session.id)
    attrs = ContextAttributes(
        user_id=USER_ID,
        session_id=str(session.id),
        session_name=session.name,
        session_metadata=session.metadata,
    )
    rendered = attrs.render()
    assert "uploads" in rendered
    assert "latest_moment_id" in rendered
    assert "api-spec" in rendered


# ---------------------------------------------------------------------------
# 21. REM LOOKUP / FUZZY / SEARCH user isolation
# ---------------------------------------------------------------------------

USER_B = UUID("bbbbbbbb-0000-0000-0000-222222222222")


async def test_rem_lookup_user_isolation(db, encryption):
    """REM LOOKUP with user_id filter only returns that user's entities."""
    svc = _make_content_service(db, encryption)

    # User A uploads
    p1, p2, p3 = _patch_kreuzberg("User A secret data.", ["User A secret data."])
    with p1, p2, p3:
        await svc.ingest(b"a", "secret-a.txt", mime_type="text/plain", user_id=USER_ID)

    # User B uploads
    p1, p2, p3 = _patch_kreuzberg("User B secret data.", ["User B secret data."])
    with p1, p2, p3:
        await svc.ingest(b"b", "secret-b.txt", mime_type="text/plain", user_id=USER_B)

    # LOOKUP without user filter → finds both
    res_a = await db.rem_lookup("secret-a-chunk-0000")
    res_b = await db.rem_lookup("secret-b-chunk-0000")
    assert len(res_a) == 1
    assert len(res_b) == 1

    # LOOKUP with user_id=USER_A → finds A, not B
    res_a_filtered = await db.rem_lookup("secret-a-chunk-0000", user_id=USER_ID)
    res_b_filtered = await db.rem_lookup("secret-b-chunk-0000", user_id=USER_ID)
    assert len(res_a_filtered) == 1
    assert res_a_filtered[0]["data"]["content"] == "User A secret data."
    assert len(res_b_filtered) == 0  # User A can't see User B's chunk

    # LOOKUP with user_id=USER_B → finds B, not A
    res_a_as_b = await db.rem_lookup("secret-a-chunk-0000", user_id=USER_B)
    res_b_as_b = await db.rem_lookup("secret-b-chunk-0000", user_id=USER_B)
    assert len(res_a_as_b) == 0  # User B can't see User A's chunk
    assert len(res_b_as_b) == 1
    assert res_b_as_b[0]["data"]["content"] == "User B secret data."

    # LOOKUP moments — same isolation
    res_ma = await db.rem_lookup("upload-secret-a", user_id=USER_ID)
    res_mb = await db.rem_lookup("upload-secret-b", user_id=USER_ID)
    assert len(res_ma) == 1
    assert res_ma[0]["data"]["moment_type"] == "content_upload"
    assert len(res_mb) == 0  # User A can't see User B's moment


async def test_rem_fuzzy_user_isolation(db, encryption):
    """REM FUZZY with user_id only returns that user's entities."""
    svc = _make_content_service(db, encryption)

    p1, p2, p3 = _patch_kreuzberg("Quantum computing breakthroughs.", ["Quantum computing breakthroughs."])
    with p1, p2, p3:
        await svc.ingest(b"q", "quantum-paper.txt", mime_type="text/plain", user_id=USER_ID)

    p1, p2, p3 = _patch_kreuzberg("Quantum entanglement results.", ["Quantum entanglement results."])
    with p1, p2, p3:
        await svc.ingest(b"q2", "quantum-results.txt", mime_type="text/plain", user_id=USER_B)

    # FUZZY "quantum" without user filter → finds both users' data
    all_results = await db.rem_fuzzy("quantum", threshold=0.2, limit=20)
    all_keys = [r["data"]["key"] for r in all_results]
    has_a = any("quantum-paper" in k for k in all_keys)
    has_b = any("quantum-results" in k for k in all_keys)
    assert has_a and has_b, f"Expected both users' data, got: {all_keys}"

    # FUZZY with user_id=USER_A → only A's data
    a_results = await db.rem_fuzzy("quantum", user_id=USER_ID, threshold=0.2, limit=20)
    a_keys = [r["data"]["key"] for r in a_results]
    assert any("quantum-paper" in k for k in a_keys)
    assert not any("quantum-results" in k for k in a_keys), f"User A saw User B's data: {a_keys}"

    # FUZZY with user_id=USER_B → only B's data
    b_results = await db.rem_fuzzy("quantum", user_id=USER_B, threshold=0.2, limit=20)
    b_keys = [r["data"]["key"] for r in b_results]
    assert any("quantum-results" in k for k in b_keys)
    assert not any("quantum-paper" in k for k in b_keys), f"User B saw User A's data: {b_keys}"
