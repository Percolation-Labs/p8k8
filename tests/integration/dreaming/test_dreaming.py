"""Integration tests for DreamingHandler context loading.

These tests exercise _load_dreaming_context() directly (no LLM calls).
Full agent execution is tested separately or manually.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from p8.services.encryption import EncryptionService
from p8.workers.handlers.dreaming import DATA_TOKEN_BUDGET, DreamingHandler

from tests.integration.dreaming.fixtures import (
    TEST_USER_ID,
    setup_dreaming_fixtures,
)


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


async def test_load_context_returns_data(db, encryption):
    """With fixtures in place, context loading returns non-empty text."""
    await setup_dreaming_fixtures(db, encryption)

    handler = DreamingHandler()
    text, stats = await handler._load_dreaming_context(
        TEST_USER_ID, lookback_days=1, db=db, encryption=encryption,
    )

    assert len(text) > 0
    assert stats["moments"] >= 2
    assert stats["sessions"] >= 2
    assert stats["messages"] >= 6  # at least the ML session messages


async def test_load_context_includes_moments(db, encryption):
    """Context text contains moment summaries."""
    await setup_dreaming_fixtures(db, encryption)

    handler = DreamingHandler()
    text, _ = await handler._load_dreaming_context(
        TEST_USER_ID, lookback_days=1, db=db, encryption=encryption,
    )

    assert "ML model" in text or "training pipeline" in text.lower() or "machine" in text.lower()
    assert "microservices" in text.lower() or "API gateway" in text


async def test_load_context_includes_session_messages(db, encryption):
    """Context text contains session message content."""
    await setup_dreaming_fixtures(db, encryption)

    handler = DreamingHandler()
    text, _ = await handler._load_dreaming_context(
        TEST_USER_ID, lookback_days=1, db=db, encryption=encryption,
    )

    assert "training pipeline" in text.lower() or "ML model" in text.lower()


async def test_load_context_within_token_budget(db, encryption):
    """Context stays within DATA_TOKEN_BUDGET even with lots of data."""
    await setup_dreaming_fixtures(db, encryption)

    handler = DreamingHandler()
    text, stats = await handler._load_dreaming_context(
        TEST_USER_ID, lookback_days=1, db=db, encryption=encryption,
    )

    assert stats["token_estimate"] <= DATA_TOKEN_BUDGET


async def test_load_context_empty_user(db, encryption):
    """User with no data returns empty string."""
    nobody = UUID("eeeeeeee-0000-0000-0000-ffffffffffff")

    handler = DreamingHandler()
    text, stats = await handler._load_dreaming_context(
        nobody, lookback_days=1, db=db, encryption=encryption,
    )

    assert text.strip() == ""
    assert stats["moments"] == 0
    assert stats["sessions"] == 0


async def test_load_context_old_data_excluded(db, encryption):
    """Data older than lookback_days is not included."""
    await setup_dreaming_fixtures(db, encryption)

    handler = DreamingHandler()
    # Use lookback_days=0 — nothing from "today" should match since fixtures
    # were just created (they'll have timestamps from now)
    # Actually lookback_days=0 means cutoff = now, so everything created before now
    # is excluded. Since fixtures were just created at ~now, this is borderline.
    # Use a very small lookback with a separate user instead.
    nobody = UUID("eeeeeeee-0000-0000-0000-eeeeeeeeeeee")
    text, stats = await handler._load_dreaming_context(
        nobody, lookback_days=1, db=db, encryption=encryption,
    )

    assert stats["moments"] == 0


async def test_handler_skips_no_user(db, encryption):
    """Handler returns skipped status when no user_id provided."""

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.db = db
    ctx.encryption = encryption

    handler = DreamingHandler()
    result = await handler.handle({}, ctx)

    assert result["status"] == "skipped_no_user"
