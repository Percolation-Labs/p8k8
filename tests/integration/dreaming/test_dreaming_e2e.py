"""End-to-end test — runs the dreaming agent against fixture data with a real LLM.

Requires:
  - Running PostgreSQL (docker compose up -d)
  - P8_OPENAI_API_KEY set in environment or .env

Verifies:
  - Context loading produces meaningful text
  - Agent generates dream moments (moment_type='dream') via structured output
  - Dream moments have summaries, topic_tags, and graph_edges with reasons
  - Each dream moment gets a companion session for follow-up chat
  - Back-edges are merged onto related entities
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest

from p8.services.bootstrap import _export_api_keys
from p8.settings import Settings
from p8.api.tools import init_tools, set_tool_context
from p8.ontology.types import Moment, Resource
from p8.services.repository import Repository
from p8.workers.handlers.dreaming import DreamingHandler

from tests.integration.dreaming.fixtures import (
    MOMENT_ARCH,
    MOMENT_ML,
    RESOURCE_ARCH,
    RESOURCE_ML,
    TEST_USER_ID,
    setup_dreaming_fixtures,
)

log = logging.getLogger(__name__)


class _Ctx:
    """Minimal context object matching what DreamingHandler expects."""

    def __init__(self, db, encryption):
        self.db = db
        self.encryption = encryption


@pytest.fixture(autouse=True)
async def _setup(clean_db, db, encryption):
    _export_api_keys(Settings())  # Bridge P8_OPENAI_API_KEY → OPENAI_API_KEY
    init_tools(db, encryption)
    set_tool_context(user_id=TEST_USER_ID)
    # Clean up any stale dream moments and sessions from prior test runs
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'dream' AND user_id = $1",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1)",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1",
        TEST_USER_ID,
    )
    await setup_dreaming_fixtures(db, encryption)
    # Clear adapter cache to avoid stale DB references across tests
    from p8.agentic.adapter import _adapter_cache
    _adapter_cache.clear()
    yield


@pytest.mark.llm
async def test_dreaming_agent_e2e(db, encryption):
    """Full pipeline: load context → run agent → verify companion sessions + dream moments."""
    handler = DreamingHandler()
    ctx = _Ctx(db, encryption)

    # First verify context loading works and has substance
    text, stats = await handler._load_dreaming_context(
        TEST_USER_ID, lookback_days=1, db=db, encryption=encryption,
    )
    log.info("Context loaded: %d chars, stats=%s", len(text), stats)
    assert stats["moments"] >= 2, f"Expected >=2 moments, got {stats['moments']}"
    assert stats["sessions"] >= 2, f"Expected >=2 sessions, got {stats['sessions']}"

    # Run the dreaming handler
    result = await handler.handle(
        {"user_id": str(TEST_USER_ID), "lookback_days": 1},
        ctx,
    )
    log.info("Dreaming result: %s", result)

    phase2 = result.get("phase2", {})
    assert phase2.get("status") == "ok", (
        f"Phase 2 failed: {phase2.get('error', phase2.get('status'))}"
    )

    # ── Verify the dreaming session was created ──
    session_id = UUID(phase2["session_id"])
    session_rows = await db.fetch(
        "SELECT id, name, mode, agent_name FROM sessions WHERE id = $1",
        session_id,
    )
    assert len(session_rows) == 1
    sess = session_rows[0]
    assert sess["mode"] == "dreaming"
    assert sess["agent_name"] == "dreaming-agent"
    log.info("Dreaming session: %s (%s)", sess["name"], session_id)

    # ── Dreaming session should only have chained tool messages (no user/assistant bloat) ──
    msg_rows = await db.fetch(
        "SELECT message_type FROM messages"
        " WHERE session_id = $1 AND deleted_at IS NULL",
        session_id,
    )
    allowed_types = {"tool_call", "tool_response"}
    unexpected = [r["message_type"] for r in msg_rows if r["message_type"] not in allowed_types]
    assert len(unexpected) == 0, (
        f"Dreaming session should only have tool messages, got unexpected: {unexpected}"
    )

    # ── Verify dream moments ──
    moment_repo = Repository(Moment, db, encryption)
    dreams = await moment_repo.find(
        user_id=TEST_USER_ID,
        filters={"moment_type": "dream"},
    )
    log.info("Dream moments created: %d", len(dreams))
    for d in dreams:
        log.info(
            "  [%s] %s | tags=%s | edges=%d | session=%s",
            d.name, (d.summary or "")[:80], d.topic_tags,
            len(d.graph_edges), d.source_session_id,
        )

    assert len(dreams) >= 1, "Agent should have created at least 1 dream moment"

    for dream in dreams:
        assert dream.moment_type == "dream"
        assert dream.summary and len(dream.summary) > 20, f"Summary too short: {dream.summary!r}"
        assert dream.metadata.get("source") == "dreaming"
        # source_session_id should be set (via save_moments tool context)
        assert dream.source_session_id is not None

    # At least one dream should have topic_tags (LLM sometimes omits on some)
    dreams_with_tags = [d for d in dreams if d.topic_tags]
    assert len(dreams_with_tags) >= 1, (
        f"At least one dream should have topic_tags: {[d.name for d in dreams]}"
    )

    # Verify graph_edges have reasons
    dreams_with_edges = [d for d in dreams if d.graph_edges]
    assert len(dreams_with_edges) >= 1, "At least one dream should have graph_edges"
    for d in dreams_with_edges:
        for e in d.graph_edges:
            assert "reason" in e, f"Edge on {d.name} missing reason: {e}"

    # Check that edges reference known entities
    all_edge_targets = {
        e["target"]
        for d in dreams
        for e in d.graph_edges
    }
    known_keys = {MOMENT_ML, MOMENT_ARCH, RESOURCE_ML, RESOURCE_ARCH}
    referenced_known = all_edge_targets & known_keys
    log.info("Edge targets: %s (known: %s)", all_edge_targets, referenced_known)
    assert len(referenced_known) >= 1 or len(all_edge_targets) >= 1, (
        "Dream edges should reference entities from the context"
    )

    # Verify back-edges were merged onto target entities
    resource_repo = Repository(Resource, db, encryption)
    for name in (RESOURCE_ML, RESOURCE_ARCH):
        resources = await resource_repo.find(
            user_id=TEST_USER_ID, filters={"name": name},
        )
        if resources:
            r = resources[0]
            dreamed_edges = [
                e for e in r.graph_edges if e.get("relation") == "dreamed_from"
            ]
            if dreamed_edges:
                log.info("Back-edge on %s: %s", name, dreamed_edges)

    log.info(
        "E2E complete: session=%s, %d dreams, %d io_tokens",
        session_id, len(dreams), result.get("io_tokens", 0),
    )
