"""Tests for Phase 1 dreaming — session consolidation + resource enrichment.

No LLM calls. Tests _build_session_moments() and _enrich_moment_with_resources().
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from p8.ontology.types import Moment, Resource, Session
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.parsing import ensure_parsed
from p8.utils.tokens import estimate_tokens
from p8.workers.handlers import dreaming as dreaming_mod
from p8.workers.handlers.dreaming import DreamingHandler

TEST_USER_ID = UUID("dddddddd-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


async def _create_chatty_session(db, encryption, *, user_id=TEST_USER_ID) -> Session:
    """Create a session with enough messages to exceed a low token threshold."""
    session_repo = Repository(Session, db, encryption)
    session = Session(name=f"phase1-test-{uuid4().hex[:8]}", mode="chat", user_id=user_id)
    [session] = await session_repo.upsert(session)

    memory = MemoryService(db, encryption)
    # Generate enough messages to exceed threshold=100 tokens
    for i in range(20):
        content = f"Message {i}: This is a longer test message to accumulate token count for phase 1 testing purposes."
        await memory.persist_message(
            session.id, "user" if i % 2 == 0 else "assistant", content,
            user_id=user_id, token_count=estimate_tokens(content),
        )
    return session


async def _attach_upload(db, encryption, session_id: UUID, *, user_id=TEST_USER_ID):
    """Create a content_upload moment and matching resource for the session."""
    resource_name = f"test-doc-chunk-0000"
    resource_content = "Revenue grew 23% YoY. Operating expenses down 8%. Free cash flow reached 120M."

    resource_repo = Repository(Resource, db, encryption)
    resource = Resource(
        name=resource_name, content=resource_content,
        category="document", user_id=user_id,
    )
    [resource] = await resource_repo.upsert(resource)

    moment_repo = Repository(Moment, db, encryption)
    upload_moment = Moment(
        name="upload-test-doc-dreaming",
        moment_type="content_upload",
        summary=f"Uploaded test-doc.txt (1 chunks).\nResources: {resource_name}",
        source_session_id=session_id,
        user_id=user_id,
        metadata={
            "source": "upload",
            "file_name": "test-doc.txt",
            "resource_keys": [resource_name],
        },
    )
    [upload_moment] = await moment_repo.upsert(upload_moment)
    return resource, upload_moment


async def test_build_session_moments_creates_chunks(db, encryption, monkeypatch):
    """Phase 1 builds session_chunk moments from sessions exceeding threshold."""
    # Lower threshold so our test messages are enough
    monkeypatch.setattr(dreaming_mod, "PHASE1_THRESHOLD", 100)

    session = await _create_chatty_session(db, encryption)

    handler = DreamingHandler()
    result = await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    assert result["sessions_checked"] >= 1
    assert result["moments_built"] >= 1

    # Verify the moment exists
    rows = await db.fetch(
        "SELECT name, moment_type, summary FROM moments"
        " WHERE source_session_id = $1 AND moment_type = 'session_chunk'"
        " AND deleted_at IS NULL",
        session.id,
    )
    assert len(rows) >= 1
    assert rows[0]["moment_type"] == "session_chunk"
    assert len(rows[0]["summary"]) > 0


async def test_build_session_moments_skips_dreaming_sessions(db, encryption, monkeypatch):
    """Phase 1 skips sessions with mode='dreaming'."""
    monkeypatch.setattr(dreaming_mod, "PHASE1_THRESHOLD", 100)

    session_repo = Repository(Session, db, encryption)
    dreaming_session = Session(
        name="dreaming-session-test", mode="dreaming", user_id=TEST_USER_ID,
    )
    [dreaming_session] = await session_repo.upsert(dreaming_session)

    memory = MemoryService(db, encryption)
    for i in range(10):
        await memory.persist_message(
            dreaming_session.id, "user", f"Dreaming message {i} with enough content to accumulate tokens.",
            user_id=TEST_USER_ID, token_count=50,
        )

    handler = DreamingHandler()
    result = await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    # Should NOT have built a moment for the dreaming session
    rows = await db.fetch(
        "SELECT id FROM moments"
        " WHERE source_session_id = $1 AND moment_type = 'session_chunk'"
        " AND deleted_at IS NULL",
        dreaming_session.id,
    )
    assert len(rows) == 0


async def test_build_session_moments_below_threshold(db, encryption):
    """Phase 1 does NOT build a moment when tokens are below threshold (6000 default)."""
    session_repo = Repository(Session, db, encryption)
    session = Session(name="small-session", mode="chat", user_id=TEST_USER_ID)
    [session] = await session_repo.upsert(session)

    memory = MemoryService(db, encryption)
    # Only a few messages — well below 6000 tokens
    await memory.persist_message(session.id, "user", "Hello", user_id=TEST_USER_ID)
    await memory.persist_message(session.id, "assistant", "Hi there", user_id=TEST_USER_ID)

    handler = DreamingHandler()
    result = await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    assert result["sessions_checked"] >= 1
    assert result["moments_built"] == 0


async def test_enrich_moment_with_resources(db, encryption, monkeypatch):
    """Resource enrichment appends [Uploaded Resources] to session_chunk moment."""
    monkeypatch.setattr(dreaming_mod, "PHASE1_THRESHOLD", 100)

    session = await _create_chatty_session(db, encryption)
    resource, upload_moment = await _attach_upload(db, encryption, session.id)

    handler = DreamingHandler()
    result = await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    assert result["moments_built"] >= 1

    # Check the session_chunk moment was enriched
    rows = await db.fetch(
        "SELECT summary, metadata FROM moments"
        " WHERE source_session_id = $1 AND moment_type = 'session_chunk'"
        " AND deleted_at IS NULL",
        session.id,
    )
    assert len(rows) >= 1
    summary = rows[0]["summary"]
    metadata = ensure_parsed(rows[0]["metadata"], default={})

    assert "[Uploaded Resources]" in summary
    assert "test-doc-chunk-0000" in summary
    assert "Revenue grew" in summary
    assert "resource_keys" in metadata
    assert "test-doc-chunk-0000" in metadata["resource_keys"]


async def test_enrich_skips_non_chunk0_resources(db, encryption, monkeypatch):
    """Only chunk-0000 resource keys are included in enrichment."""
    monkeypatch.setattr(dreaming_mod, "PHASE1_THRESHOLD", 100)

    session = await _create_chatty_session(db, encryption)

    # Create upload moment with chunk-0001 key (not chunk-0000)
    moment_repo = Repository(Moment, db, encryption)
    upload_moment = Moment(
        name="upload-other-doc",
        moment_type="content_upload",
        summary="Uploaded other-doc.txt",
        source_session_id=session.id,
        user_id=TEST_USER_ID,
        metadata={"resource_keys": ["other-doc-chunk-0001"]},
    )
    await moment_repo.upsert(upload_moment)

    handler = DreamingHandler()
    await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    rows = await db.fetch(
        "SELECT summary FROM moments"
        " WHERE source_session_id = $1 AND moment_type = 'session_chunk'"
        " AND deleted_at IS NULL",
        session.id,
    )
    assert len(rows) >= 1
    # Should NOT contain [Uploaded Resources] since no chunk-0000 keys
    assert "[Uploaded Resources]" not in rows[0]["summary"]


async def test_enrich_no_uploads(db, encryption, monkeypatch):
    """Session without uploads — moment summary is unchanged."""
    monkeypatch.setattr(dreaming_mod, "PHASE1_THRESHOLD", 100)

    session = await _create_chatty_session(db, encryption)

    handler = DreamingHandler()
    await handler._build_session_moments(TEST_USER_ID, None, db, encryption)

    rows = await db.fetch(
        "SELECT summary FROM moments"
        " WHERE source_session_id = $1 AND moment_type = 'session_chunk'"
        " AND deleted_at IS NULL",
        session.id,
    )
    assert len(rows) >= 1
    assert "[Uploaded Resources]" not in rows[0]["summary"]


async def test_empty_user_phase1(db, encryption):
    """Phase 1 for a user with no sessions returns zeros."""
    nobody = UUID("eeeeeeee-0000-0000-0000-ffffffffffff")

    handler = DreamingHandler()
    result = await handler._build_session_moments(nobody, None, db, encryption)

    assert result["sessions_checked"] == 0
    assert result["moments_built"] == 0
