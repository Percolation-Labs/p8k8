"""End-to-end memory pipeline tests — compaction, moments, injection, uploads.

Uses low thresholds and realistic example data from tests/data/ to verify
the full memory pipeline works without gaps.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from p8.ontology.types import Message, Moment, Session
from p8.services.memory import MemoryService
from p8.services.repository import Repository


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


async def _create_session(db, encryption, *, total_tokens: int = 0) -> Session:
    repo = Repository(Session, db, encryption)
    session = Session(name=f"test-session-{uuid4()}", mode="chat", total_tokens=total_tokens)
    [result] = await repo.upsert(session)
    return result


async def _add_messages(
    memory: MemoryService,
    session_id,
    messages: list[dict],
) -> list[Message]:
    """Persist a list of {role, content, tokens} dicts as messages."""
    results = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        tokens = msg.get("tokens", len(content) // 4)
        result = await memory.persist_message(
            session_id, role, content, token_count=tokens,
        )
        results.append(result)
    return results


async def _add_alternating(
    memory: MemoryService,
    session_id,
    count: int,
    *,
    token_count: int = 50,
    prefix: str = "msg",
) -> list[Message]:
    """Persist alternating user/assistant messages."""
    results = []
    for i in range(count):
        msg_type = "user" if i % 2 == 0 else "assistant"
        content = f"{prefix}-{i}: " + ("x" * (token_count * 4))
        msg = await memory.persist_message(
            session_id, msg_type, content, token_count=token_count,
        )
        results.append(msg)
    return results


# ---------------------------------------------------------------------------
# 1. Compaction produces resolvable breadcrumbs
# ---------------------------------------------------------------------------


async def test_compaction_produces_resolvable_breadcrumbs(db, encryption):
    """After compaction, old assistant messages contain [REM LOOKUP session-{id}-chunk-{N}]
    and the moment name IS in kv_store (resolvable via rem_lookup)."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add 10 messages (5 user + 5 assistant) × 50 tokens = 500 total
    await _add_alternating(memory, session.id, 10, token_count=50)

    # Build a moment manually so compaction has something to reference
    moment = await memory.build_moment(session.id)
    assert moment is not None
    moment_name = moment.name  # e.g. session-{id}-chunk-0

    # Verify the moment is in kv_store (has_kv_sync = true for moments)
    kv_row = await db.fetchrow(
        "SELECT * FROM kv_store WHERE entity_key = $1", moment_name,
    )
    assert kv_row is not None, f"Moment {moment_name} not found in kv_store"

    # Load context with small budget → forces compaction of old messages
    ctx = await memory.load_context(session.id, max_tokens=8000, always_last=3)

    # Find compacted messages
    compacted = [m for m in ctx if "[REM LOOKUP" in m.get("content", "")]
    assert len(compacted) > 0, "Expected at least one compacted message"

    # All breadcrumbs should reference the moment name, not a msg ID
    for m in compacted:
        assert moment_name in m["content"], (
            f"Breadcrumb should reference moment {moment_name}, got: {m['content']}"
        )


# ---------------------------------------------------------------------------
# 2. Multi-turn session with moments
# ---------------------------------------------------------------------------


async def test_multi_turn_session_with_moments(db, encryption):
    """Simulate 15+ messages across 3 batches. Threshold triggers 2 moments.
    Context includes moment summaries + recent messages. Total fits within budget."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)
    threshold = 200

    # Batch 1: 6 messages × 50 = 300 tokens → triggers moment 1
    await _add_alternating(memory, session.id, 6, token_count=50, prefix="b1")
    m1 = await memory.maybe_build_moment(session.id, threshold=threshold)
    assert m1 is not None
    assert m1.metadata["chunk_index"] == 0

    # Batch 2: 6 more × 50 = 300 tokens → triggers moment 2
    await _add_alternating(memory, session.id, 6, token_count=50, prefix="b2")
    m2 = await memory.maybe_build_moment(session.id, threshold=threshold)
    assert m2 is not None
    assert m2.metadata["chunk_index"] == 1
    assert m2.previous_moment_keys == [m1.name]

    # Batch 3: 3 more × 50 = 150 tokens → below threshold of 200
    await _add_alternating(memory, session.id, 3, token_count=50, prefix="b3")
    m3 = await memory.maybe_build_moment(session.id, threshold=threshold)
    assert m3 is None, "150 tokens should be below threshold of 200"

    # Load context — should include moment summaries + recent messages
    ctx = await memory.load_context(session.id, max_moments=3, always_last=5)

    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 2, "Should have at least 2 moment summaries"

    # Recent messages should be verbatim (last 5)
    non_system = [m for m in ctx if m.get("message_type") != "system"]
    recent = non_system[-5:]
    for msg in recent:
        assert "[REM LOOKUP" not in msg.get("content", ""), "Recent messages should not be compacted"


# ---------------------------------------------------------------------------
# 3. Moments visible after pai_messages path
# ---------------------------------------------------------------------------


async def test_moment_visible_after_pai_messages_path(db, encryption):
    """Create session with pai_messages in metadata, verify moments still appear
    in load_history() output."""
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        UserPromptPart,
    )

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema

    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Create a moment for this session
    moment_repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=f"session-{session.id}-chunk-0",
        moment_type="session_chunk",
        summary="Discussed TiDB migration plan and Qdrant setup.",
        source_session_id=session.id,
        metadata={"message_count": 5, "token_count": 300, "chunk_index": 0},
    )
    await moment_repo.upsert(moment)

    # Store pai_messages in session metadata (simulates persist_turn path)
    pai_messages = [
        ModelRequest(parts=[UserPromptPart(content="Hello")]),
        ModelResponse(parts=[TextPart(content="Hi there!")]),
    ]
    pai_json = ModelMessagesTypeAdapter.dump_json(pai_messages).decode()
    await db.execute(
        "UPDATE sessions SET metadata = jsonb_set("
        "  COALESCE(metadata, '{}'::jsonb),"
        "  '{pai_messages}',"
        "  $1::jsonb"
        ") WHERE id = $2",
        pai_json,
        session.id,
    )

    # Create a minimal agent adapter
    schema = Schema(
        name="test-agent", kind="agent",
        content="Test agent", json_schema={},
    )
    adapter = AgentAdapter(schema, db, encryption)

    # load_history should include moment injection even via pai_messages path
    messages = await adapter.load_history(session.id)
    assert len(messages) >= 3, f"Expected >= 3 messages (1 moment + 2 pai), got {len(messages)}"

    # First message should be the moment injection
    first = messages[0]
    assert hasattr(first, "parts")
    first_content = first.parts[0].content
    assert "Session context" in first_content
    assert "TiDB" in first_content


# ---------------------------------------------------------------------------
# 4. Compaction preserves recent messages
# ---------------------------------------------------------------------------


async def test_compaction_preserves_recent_messages(db, encryption):
    """After compaction, the last N messages are verbatim (not breadcrumbed)."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # Add 12 messages with identifiable content
    messages_data = []
    for i in range(12):
        role = "user" if i % 2 == 0 else "assistant"
        messages_data.append({"role": role, "content": f"unique-message-{i:03d}", "tokens": 50})
    await _add_messages(memory, session.id, messages_data)

    # Build a moment so compaction uses moment breadcrumbs
    await memory.build_moment(session.id)

    always_last = 5
    ctx = await memory.load_context(session.id, max_tokens=8000, always_last=always_last)

    # Last `always_last` non-system messages should be verbatim
    non_system = [m for m in ctx if m.get("message_type") != "system"]
    recent = non_system[-always_last:]

    for msg in recent:
        content = msg.get("content", "")
        assert "unique-message-" in content, f"Recent message should be verbatim, got: {content[:50]}"
        assert "[REM LOOKUP" not in content
        assert "[earlier message compacted]" not in content


# ---------------------------------------------------------------------------
# 5. Upload moment in session context
# ---------------------------------------------------------------------------


async def test_upload_moment_in_session_context(db, encryption):
    """Upload file with session_id → load_context() includes upload moment."""
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock, patch

    from p8.services.content import ContentService
    from p8.services.files import FileService

    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

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
        content="Report about Q4 financials and revenue growth.",
        chunks=[_FakeChunk("Q4 revenue was up 20%.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        await svc.ingest(
            b"pdf bytes", "q4-report.pdf",
            mime_type="application/pdf",
            session_id=str(session.id),
        )

    # Add a message so load_context has something to return
    await memory.persist_message(session.id, "user", "What's in the Q4 report?", token_count=15)

    # load_context should include the upload moment
    ctx = await memory.load_context(session.id, max_moments=5)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    upload_msgs = [m for m in system_msgs if "q4-report.pdf" in m.get("content", "")]
    assert len(upload_msgs) >= 1, "Upload moment should appear in session context"


# ---------------------------------------------------------------------------
# 6. Upload moment without session still findable
# ---------------------------------------------------------------------------


async def test_upload_moment_without_session_creates_session(db, encryption):
    """Upload without session_id → creates a session, links moment to it."""
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
        content="Meeting notes from sprint planning.",
        chunks=[_FakeChunk("Discussed TiDB migration.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        # No session_id
        result = await svc.ingest(b"text bytes", "meeting-notes.txt", mime_type="text/plain")

    # Session should be created and returned
    assert result.session_id is not None

    # Moment should exist and be linked to the new session
    rows = await db.fetch(
        "SELECT * FROM moments WHERE name = 'upload-meeting-notes' AND deleted_at IS NULL"
    )
    assert len(rows) == 1
    assert rows[0]["source_session_id"] == result.session_id
    moment_id = rows[0]["id"]

    # Session should have upload metadata
    session_row = await db.fetchrow(
        "SELECT * FROM sessions WHERE id = $1", result.session_id,
    )
    assert session_row is not None
    assert session_row["name"] == "upload: meeting-notes.txt"
    assert "resource_keys" in session_row["metadata"]

    # Should be in kv_store (findable via rem_lookup)
    kv_row = await db.fetchrow(
        "SELECT * FROM kv_store WHERE entity_id = $1", moment_id,
    )
    assert kv_row is not None


# ---------------------------------------------------------------------------
# 7. Moment chaining across many batches
# ---------------------------------------------------------------------------


async def test_moment_chaining_across_many_batches(db, encryption):
    """5 batches of messages → 5 moments → each chains to predecessor."""
    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)
    threshold = 80
    moments = []

    for batch in range(5):
        await _add_alternating(
            memory, session.id, 4, token_count=30, prefix=f"batch{batch}",
        )
        moment = await memory.maybe_build_moment(session.id, threshold=threshold)
        assert moment is not None, f"Batch {batch} should trigger a moment"
        moments.append(moment)

    # Verify chain
    assert moments[0].previous_moment_keys == []
    for i in range(1, 5):
        assert moments[i].previous_moment_keys == [moments[i - 1].name], (
            f"Moment {i} should chain to moment {i-1}"
        )
        assert moments[i].metadata["chunk_index"] == i

    # Verify all moments are in kv_store
    for m in moments:
        kv_row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_key = $1", m.name)
        assert kv_row is not None, f"Moment {m.name} should be in kv_store"


# ---------------------------------------------------------------------------
# 8. Full pipeline with example data
# ---------------------------------------------------------------------------


async def test_full_pipeline_with_example_data(db, encryption):
    """Load conversations from tests/data/conversations.json, replay through
    memory service, verify moments + compaction + context assembly."""
    memory = MemoryService(db, encryption)

    conversations_path = DATA_DIR / "conversations.json"
    with open(conversations_path) as f:
        data = json.load(f)

    convo = data["conversations"][0]  # "project-planning"
    assert convo["name"] == "project-planning"

    session = await _create_session(db, encryption)
    threshold = convo["token_threshold"]

    # Replay all messages
    await _add_messages(memory, session.id, convo["messages"])

    # Repeatedly check for moments (simulating end-of-turn triggers)
    moments_created = []
    for _ in range(5):  # at most 5 checks
        m = await memory.maybe_build_moment(session.id, threshold=threshold)
        if m:
            moments_created.append(m)
        else:
            break

    assert len(moments_created) >= 1, (
        f"Expected at least 1 moment for {convo['name']}, got {len(moments_created)}"
    )

    # Load context and verify structure
    ctx = await memory.load_context(session.id, max_moments=3, always_last=3)
    assert len(ctx) > 0

    # Should have moment summaries + messages
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]
    assert len(system_msgs) >= 1, "Should have at least 1 moment summary"

    # Verify content from the conversation appears somewhere
    all_content = " ".join(m.get("content", "") for m in ctx)
    assert "Aurora" in all_content or "REM LOOKUP" in all_content


# ---------------------------------------------------------------------------
# 9. Upload then session context
# ---------------------------------------------------------------------------


async def test_upload_then_session_context(db, encryption):
    """Upload file in a session, then add chat messages → both upload moment
    and session_chunk moments appear in context."""
    import asyncio
    from dataclasses import dataclass
    from unittest.mock import AsyncMock, MagicMock, patch

    from p8.services.content import ContentService
    from p8.services.files import FileService

    memory = MemoryService(db, encryption)
    session = await _create_session(db, encryption)

    # 1. Upload a file with session_id
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
        content="Sample report content for testing pipeline.",
        chunks=[_FakeChunk("Sample report chunk.")],
    )

    with (
        patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
        patch("kreuzberg.ChunkingConfig"),
        patch("kreuzberg.ExtractionConfig"),
    ):
        await svc.ingest(
            b"report bytes", "sample-report.txt",
            mime_type="text/plain",
            session_id=str(session.id),
        )

    # Small delay so timestamps differ
    await asyncio.sleep(0.05)

    # 2. Add chat messages that trigger a session_chunk moment
    await _add_alternating(memory, session.id, 8, token_count=50, prefix="chat")
    chunk_moment = await memory.maybe_build_moment(session.id, threshold=200)
    assert chunk_moment is not None

    # 3. Load context — should include BOTH upload and session_chunk moments
    ctx = await memory.load_context(session.id, max_moments=5)
    system_msgs = [m for m in ctx if m.get("message_type") == "system"]

    contents = " ".join(m.get("content", "") for m in system_msgs)
    assert "sample-report.txt" in contents, "Upload moment should appear in context"
    # Session chunk moment should also be present (contains assistant text from chat)
    assert len(system_msgs) >= 2, (
        f"Expected at least 2 system messages (upload + chunk), got {len(system_msgs)}"
    )
