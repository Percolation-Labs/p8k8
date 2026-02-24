"""Integration tests for rem_moments_feed — paginated feed with virtual daily summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from p8.ontology.types import Message, Moment, Session
from p8.services.repository import Repository


DATA_DIR = Path(__file__).parent / "data"
USER_ID = UUID("cccccccc-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_seed(db, encryption, *, user_id: UUID | None = None):
    """Insert seed-feed.json into the database with realistic timestamps.

    Messages and moments use ``days_ago`` to compute ``created_at`` relative
    to the current date so the feed always has "today", "yesterday", etc.
    """
    raw = json.loads((DATA_DIR / "seed-feed.json").read_text())

    # Clean prior run's messages and moments for these sessions
    for s in raw["sessions"]:
        await db.execute("DELETE FROM messages WHERE session_id = $1", UUID(s["id"]))
    for mo in raw["moments"]:
        await db.execute("DELETE FROM moments WHERE name = $1", mo["name"])

    session_repo = Repository(Session, db, encryption)
    for s in raw["sessions"]:
        session = Session(
            id=UUID(s["id"]),
            name=s["name"],
            agent_name=s["agent_name"],
            mode=s["mode"],
            total_tokens=s["total_tokens"],
            user_id=user_id,
        )
        await session_repo.upsert(session)

    # Insert messages with backdated created_at via raw SQL
    now = datetime.now(UTC)
    for m in raw["messages"]:
        days_ago = m.get("days_ago", 0)
        ts = (now - timedelta(days=days_ago)).replace(microsecond=0)
        await db.execute(
            "INSERT INTO messages (id, session_id, message_type, content, token_count, user_id, created_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid4(),
            UUID(m["session_id"]),
            m["message_type"],
            m["content"],
            m["token_count"],
            user_id,
            ts,
        )

    moment_repo = Repository(Moment, db, encryption)
    for mo in raw["moments"]:
        days_ago = mo.get("days_ago", 0)
        ts = (now - timedelta(days=days_ago)).replace(microsecond=0)
        moment = Moment(
            name=mo["name"],
            moment_type=mo["moment_type"],
            summary=mo["summary"],
            source_session_id=UUID(mo["source_session_id"]),
            metadata=mo["metadata"],
            user_id=user_id,
            created_at=ts,
        )
        await moment_repo.upsert(moment)
        # Backdate via raw SQL since upsert sets created_at to now
        await db.execute(
            "UPDATE moments SET created_at = $1 WHERE name = $2",
            ts,
            mo["name"],
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_feed_returns_daily_summaries_and_moments(db, encryption):
    """Feed should contain both daily_summary and moment rows."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    assert len(feed) > 0

    event_types = {r["event_type"] for r in feed}
    assert "daily_summary" in event_types
    assert "moment" in event_types


async def test_feed_has_one_daily_summary_per_active_date(db, encryption):
    """Each date with activity should produce exactly one daily_summary."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    summaries = [r for r in feed if r["event_type"] == "daily_summary"]

    # Seed has 3 active dates: today, yesterday, 3 days ago
    assert len(summaries) == 3

    # Each should have a unique date
    dates = [r["event_date"] for r in summaries]
    assert len(set(dates)) == 3


async def test_daily_summary_metadata_has_stats(db, encryption):
    """Daily summary metadata should contain message_count, total_tokens, session_count, moment_count."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    summaries = [r for r in feed if r["event_type"] == "daily_summary"]

    for s in summaries:
        meta = s["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert "message_count" in meta
        assert "total_tokens" in meta
        assert "session_count" in meta
        assert "moment_count" in meta
        assert "sessions" in meta
        assert meta["message_count"] > 0
        assert meta["total_tokens"] > 0


async def test_daily_summary_has_deterministic_session_id(db, encryption):
    """Same user+date should always produce the same session_id (uuid_generate_v5)."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed1 = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    feed2 = await db.rem_moments_feed(user_id=USER_ID, limit=20)

    summaries1 = {r["event_date"]: r["session_id"] for r in feed1 if r["event_type"] == "daily_summary"}
    summaries2 = {r["event_date"]: r["session_id"] for r in feed2 if r["event_type"] == "daily_summary"}

    assert summaries1 == summaries2

    # All session IDs should be valid UUIDs and unique per date
    ids = list(summaries1.values())
    assert len(set(ids)) == len(ids)


async def test_feed_ordered_newest_first(db, encryption):
    """Feed should be ordered by date descending (newest first)."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    dates = [r["event_date"] for r in feed]

    # Dates should be in non-ascending order (newest first, with same-date items grouped)
    for i in range(len(dates) - 1):
        assert dates[i] >= dates[i + 1]


async def test_daily_summary_sorts_before_moments_on_same_date(db, encryption):
    """On a given date, the daily_summary should appear before real moments."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)

    # Group by date
    from collections import defaultdict
    by_date = defaultdict(list)
    for r in feed:
        by_date[r["event_date"]].append(r)

    for date, rows in by_date.items():
        summaries_in_date = [i for i, r in enumerate(rows) if r["event_type"] == "daily_summary"]
        moments_in_date = [i for i, r in enumerate(rows) if r["event_type"] == "moment"]
        if summaries_in_date and moments_in_date:
            # Summary index should be less than all moment indices
            assert max(summaries_in_date) < min(moments_in_date), (
                f"On {date}, daily_summary at index {summaries_in_date} "
                f"should appear before moments at index {moments_in_date}"
            )


async def test_feed_cursor_pagination(db, encryption):
    """Cursor-based pagination: second page uses before_date from first page."""
    await _load_seed(db, encryption, user_id=USER_ID)

    # First page: limit to 1 active date
    page1 = await db.rem_moments_feed(user_id=USER_ID, limit=1)
    assert len(page1) > 0

    # Extract oldest date from page 1 as cursor
    oldest_date = min(r["event_date"] for r in page1)
    # Subtract one day to get the next page
    cursor = (oldest_date - timedelta(days=1)).isoformat()

    page2 = await db.rem_moments_feed(user_id=USER_ID, limit=1, before_date=cursor)
    assert len(page2) > 0

    # Pages should cover different dates
    page1_dates = {r["event_date"] for r in page1}
    page2_dates = {r["event_date"] for r in page2}
    assert page1_dates.isdisjoint(page2_dates)

    # Page 2 dates should all be older than page 1 dates
    assert max(page2_dates) < min(page1_dates)


async def test_feed_user_scoped(db, encryption):
    """Feed with a user_id should not return data from other users."""
    other_user = UUID("dddddddd-0000-0000-0000-000000000099")
    await _load_seed(db, encryption, user_id=USER_ID)

    # Other user should see empty feed
    feed = await db.rem_moments_feed(user_id=other_user, limit=20)
    assert len(feed) == 0


async def test_feed_no_user_returns_all(db, encryption):
    """Feed without user_id should return all data."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=None, limit=20)
    assert len(feed) > 0

    summaries = [r for r in feed if r["event_type"] == "daily_summary"]
    assert len(summaries) >= 3  # at least the 3 seed dates, possibly more from other tests


async def test_feed_empty_for_unknown_user(db, encryption):
    """User with no data → empty feed."""
    nobody = UUID("eeeeeeee-0000-0000-0000-000000000099")
    await _load_seed(db, encryption, user_id=USER_ID)
    feed = await db.rem_moments_feed(user_id=nobody, limit=20)
    assert feed == []


async def test_today_summary_text(db, encryption):
    """Today's daily_summary should contain 'Today' in the summary text."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    today = datetime.now(UTC).date()
    today_summaries = [
        r for r in feed
        if r["event_type"] == "daily_summary" and r["event_date"] == today
    ]
    assert len(today_summaries) == 1
    assert "Today" in today_summaries[0]["summary"]


async def test_yesterday_summary_text(db, encryption):
    """Yesterday's daily_summary should contain 'Yesterday' in the summary text."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    yesterday_summaries = [
        r for r in feed
        if r["event_type"] == "daily_summary" and r["event_date"] == yesterday
    ]
    assert len(yesterday_summaries) == 1
    assert "Yesterday" in yesterday_summaries[0]["summary"]


async def test_feed_moment_has_session_id(db, encryption):
    """Real moments should carry their source_session_id."""
    await _load_seed(db, encryption, user_id=USER_ID)

    feed = await db.rem_moments_feed(user_id=USER_ID, limit=20)
    moments = [r for r in feed if r["event_type"] == "moment"]
    assert len(moments) > 0

    for m in moments:
        assert m["session_id"] is not None
