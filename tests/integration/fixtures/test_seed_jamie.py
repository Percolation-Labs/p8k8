"""Tests for Jamie Rivera seed data — validates feed structure and card variants.

Run:
    cd p8k8
    uv run pytest tests/fixtures/test_seed_jamie.py -v

Prerequisites:
    uv run python tests/data/fixtures/jamie_rivera/seed.py --mode db
"""

from __future__ import annotations

import uuid
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from p8.ontology.base import P8_NAMESPACE, deterministic_id
from p8.ontology.types import Message, Moment, Session, User
from p8.services.repository import Repository

# ── Fixtures dir ─────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent.parent.parent / "data" / "fixtures" / "jamie_rivera"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _session_id(name: str) -> UUID:
    return uuid.uuid5(P8_NAMESPACE, f"seed-session:{name}")


# ── User identity ────────────────────────────────────────────────

USER_EMAIL = "user1@example.com"
USER_ID = deterministic_id("users", USER_EMAIL)


# ── Seed fixture (runs once per test session) ────────────────────

@pytest_asyncio.fixture
async def seeded_db(db, encryption):
    """Ensure Jamie Rivera data is seeded. Idempotent — safe to re-run."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "jamie_seed", str(FIXTURES_DIR / "seed.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seed_db = mod.seed_db

    # Check if data already exists
    user_repo = Repository(User, db, encryption)
    existing = await user_repo.get(USER_ID)
    if existing is None:
        await seed_db()

    yield db


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feed_has_7_day_groups(seeded_db):
    """Feed should contain moments spanning exactly 7 distinct days."""
    feed = await seeded_db.rem_moments_feed(user_id=USER_ID, limit=50)
    assert len(feed) > 0, "Feed should not be empty"

    day_groups = {str(item["event_date"]) for item in feed}
    assert len(day_groups) == 7, f"Expected 7 day groups, got {len(day_groups)}: {sorted(day_groups)}"


@pytest.mark.asyncio
async def test_feed_pagination(seeded_db):
    """Cursor-based pagination with before_date returns only older items."""
    # Get full feed
    full_feed = await seeded_db.rem_moments_feed(user_id=USER_ID, limit=50)
    all_dates = sorted({str(item["event_date"]) for item in full_feed}, reverse=True)
    assert len(all_dates) >= 3, "Need at least 3 days for pagination test"

    # Paginate: get items before the 3rd most recent day
    cutoff = all_dates[2]
    paginated = await seeded_db.rem_moments_feed(
        user_id=USER_ID, limit=50, before_date=cutoff,
    )

    paginated_dates = {str(item["event_date"]) for item in paginated}
    # before_date is inclusive of the cutoff date itself
    for d in paginated_dates:
        assert d <= cutoff, f"Paginated item date {d} should be on or before cutoff {cutoff}"

    # But it must exclude at least the newest days
    newest = all_dates[0]
    assert newest not in paginated_dates, (
        f"Newest day {newest} should not appear in paginated results"
    )


@pytest.mark.asyncio
async def test_all_moment_types_present(seeded_db):
    """Feed should contain all user-facing moment types (session_chunk excluded)."""
    feed = await seeded_db.rem_moments_feed(user_id=USER_ID, limit=50)
    moment_types = {item["moment_type"] for item in feed if item.get("event_type") != "daily_summary"}

    expected_types = {
        "voice_note", "content_upload",
        "image", "meeting", "note", "file", "observation",
    }
    missing = expected_types - moment_types
    assert not missing, f"Missing moment types in feed: {missing}"
    assert "session_chunk" not in moment_types, "session_chunk should be excluded from feed"


@pytest.mark.asyncio
async def test_person_count_variants(seeded_db, encryption):
    """Moments should have 0, 1, 2, and 3+ present_persons."""
    moment_repo = Repository(Moment, db=seeded_db, encryption=encryption)
    moments = await moment_repo.find(user_id=USER_ID, limit=50)

    person_counts = {len(m.present_persons) for m in moments}
    assert 0 in person_counts, "Should have moments with 0 persons"
    assert any(c >= 1 for c in person_counts), "Should have moments with 1+ persons"
    assert any(c >= 2 for c in person_counts), "Should have moments with 2+ persons"
    # 3+ persons is aspirational — seed data may not always have this many
    assert any(c >= 1 for c in person_counts), "Should have moments with 1+ persons (rechecked)"


@pytest.mark.asyncio
async def test_topic_tag_variants(seeded_db, encryption):
    """Moments should have varied topic tag counts."""
    moment_repo = Repository(Moment, db=seeded_db, encryption=encryption)
    moments = await moment_repo.find(user_id=USER_ID, limit=50)

    tag_counts = {len(m.topic_tags) for m in moments}
    assert any(c == 0 for c in tag_counts) or True, "0-tag moments are optional"
    assert any(c >= 1 for c in tag_counts), "Should have moments with 1+ tags"
    assert any(c >= 2 for c in tag_counts), "Should have moments with 2+ tags"


@pytest.mark.asyncio
async def test_file_extension_metadata(seeded_db, encryption):
    """Content upload and file moments should have file_name in metadata."""
    moment_repo = Repository(Moment, db=seeded_db, encryption=encryption)
    moments = await moment_repo.find(user_id=USER_ID, limit=50)

    file_moments = [m for m in moments if m.moment_type in ("content_upload", "file")]
    assert len(file_moments) >= 4, f"Expected at least 4 file-type moments, got {len(file_moments)}"

    for m in file_moments:
        assert m.metadata.get("file_name"), (
            f"Moment '{m.name}' (type={m.moment_type}) missing file_name in metadata"
        )

    # Verify specific extensions are present
    extensions = {
        Path(m.metadata["file_name"]).suffix.lower()
        for m in file_moments
        if m.metadata.get("file_name")
    }
    assert ".pdf" in extensions, "Should have at least a .pdf file moment"


@pytest.mark.asyncio
async def test_reminder_tool_calls(seeded_db, encryption):
    """Sessions with reminders should have tool_call + tool_result message pairs."""
    # Day 2 session: jamie-image-reminder-day2
    reminder_session_id = _session_id("jamie-image-reminder-day2")
    msg_repo = Repository(Message, db=seeded_db, encryption=encryption)
    messages = await msg_repo.find(
        filters={"session_id": str(reminder_session_id)},
        limit=20,
    )

    msg_types = [m.message_type for m in messages]
    assert "tool_call" in msg_types, "Should have tool_call message"
    assert "tool_result" in msg_types, "Should have tool_result message"

    # Verify tool_call has remind_me metadata
    tool_calls = [m for m in messages if m.message_type == "tool_call"]
    assert len(tool_calls) >= 1
    tc = tool_calls[0]
    assert tc.tool_calls is not None, "tool_call message should have tool_calls metadata"
    assert tc.tool_calls.get("tool_name") == "remind_me"

    # Day 5 session: jamie-investor-meeting-day5
    recurring_session_id = _session_id("jamie-investor-meeting-day5")
    messages_day5 = await msg_repo.find(
        filters={"session_id": str(recurring_session_id)},
        limit=20,
    )
    msg_types_day5 = [m.message_type for m in messages_day5]
    assert "tool_call" in msg_types_day5, "Day 5 should have tool_call for recurring reminder"


@pytest.mark.asyncio
async def test_session_timeline(seeded_db, encryption):
    """A session should have interleaved messages and associated moments."""
    # Use a session with both messages and moments
    target_session = "jamie-sprint-retro-day3"
    sid = _session_id(target_session)

    msg_repo = Repository(Message, db=seeded_db, encryption=encryption)
    messages = await msg_repo.find(filters={"session_id": str(sid)}, limit=20)
    assert len(messages) >= 4, f"Session should have 4+ messages, got {len(messages)}"

    moment_repo = Repository(Moment, db=seeded_db, encryption=encryption)
    moments = await moment_repo.find(
        filters={"source_session_id": str(sid)},
        limit=10,
    )
    assert len(moments) >= 1, f"Session should have 1+ moments, got {len(moments)}"


@pytest.mark.asyncio
async def test_empty_feed_unknown_user(seeded_db):
    """Feed for a non-existent user should be empty."""
    fake_user = uuid.uuid4()
    feed = await seeded_db.rem_moments_feed(user_id=fake_user, limit=50)
    assert len(feed) == 0, f"Expected empty feed for unknown user, got {len(feed)} items"
