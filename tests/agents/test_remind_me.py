"""Tests for remind_me tool — scheduled reminders via pg_cron + pg_net."""

from __future__ import annotations

from uuid import UUID

import pytest

from p8.api.tools import init_tools

USER_ADA = UUID("00000000-0000-0000-0000-00000000ada0")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _setup_tools(db, encryption, clean_db):
    """Initialize tool module state with live DB + encryption."""
    init_tools(db, encryption, user_id=USER_ADA)


# ---------------------------------------------------------------------------
# remind_me — one-time (ISO datetime)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_me_onetime_iso(db):
    """One-time reminder from ISO datetime schedules a pg_cron job."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="dentist-appointment",
        description="Go to the dentist at 3pm",
        crontab="2026-04-15T15:00:00",
        tags=["health"],
        user_id=USER_ADA,
    )

    assert result["status"] == "success"
    assert result["recurrence"] == "once"
    assert result["name"] == "dentist-appointment"
    assert result["schedule"] == "0 15 15 4 *"
    assert result["reminder_id"]
    assert result["job_name"].startswith("reminder-")

    # Verify the job exists in cron.job
    row = await db.fetchrow(
        "SELECT jobname, schedule, command FROM cron.job WHERE jobname = $1",
        result["job_name"],
    )
    assert row is not None, "pg_cron job was not created"
    assert row["schedule"] == "0 15 15 4 *"
    assert "dentist-appointment" in row["command"]
    assert "Go to the dentist" in row["command"]
    assert "notifications/send" in row["command"]
    # One-time jobs include unschedule
    assert "cron.unschedule" in row["command"]

    # Cleanup
    await db.execute("SELECT cron.unschedule($1)", result["job_name"])


# ---------------------------------------------------------------------------
# remind_me — recurring (cron expression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_me_recurring_cron(db):
    """Recurring reminder from cron expression schedules a pg_cron job."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="take-vitamins",
        description="Take your daily vitamins",
        crontab="0 9 * * *",
        tags=["health", "daily"],
        user_id=USER_ADA,
    )

    assert result["status"] == "success"
    assert result["recurrence"] == "recurring"
    assert result["schedule"] == "0 9 * * *"
    assert result["next_fire"]

    # Verify the job in cron.job
    row = await db.fetchrow(
        "SELECT jobname, schedule, command FROM cron.job WHERE jobname = $1",
        result["job_name"],
    )
    assert row is not None, "pg_cron job was not created"
    assert row["schedule"] == "0 9 * * *"
    assert "take-vitamins" in row["command"]
    # Recurring jobs should NOT auto-unschedule
    assert "cron.unschedule" not in row["command"]

    # Cleanup
    await db.execute("SELECT cron.unschedule($1)", result["job_name"])


# ---------------------------------------------------------------------------
# remind_me — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_me_invalid_cron():
    """Invalid cron expression returns an error."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="bad-schedule",
        description="This should fail",
        crontab="not-a-cron",
        user_id=USER_ADA,
    )

    assert result["status"] == "error"
    assert "Invalid crontab" in result["error"]


@pytest.mark.asyncio
async def test_remind_me_missing_user():
    """Missing user_id returns an error."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="no-user",
        description="No user provided",
        crontab="0 9 * * *",
    )

    assert result["status"] == "error"
    assert "user_id" in result["error"]


# ---------------------------------------------------------------------------
# remind_me — payload structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_me_payload_in_job(db):
    """The pg_cron job command contains the correct notification payload."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="standup",
        description="Daily standup in 5 minutes",
        crontab="55 8 * * 1-5",
        tags=["work"],
        user_id=USER_ADA,
    )

    assert result["status"] == "success"

    row = await db.fetchrow(
        "SELECT command FROM cron.job WHERE jobname = $1",
        result["job_name"],
    )
    command = row["command"]

    # Verify payload structure embedded in the HTTP call
    assert str(USER_ADA) in command
    assert "standup" in command
    assert "Daily standup" in command
    assert result["reminder_id"] in command

    # Cleanup
    await db.execute("SELECT cron.unschedule($1)", result["job_name"])


# ---------------------------------------------------------------------------
# remind_me — weekly cron (e.g. every Monday at 9am)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remind_me_weekly_cron(db):
    """Weekly cron expression (e.g. Monday at 9am) works correctly."""
    from p8.api.tools.remind_me import remind_me

    result = await remind_me(
        name="weekly-review",
        description="Weekly planning review",
        crontab="0 9 * * 1",
        user_id=USER_ADA,
    )

    assert result["status"] == "success"
    assert result["recurrence"] == "recurring"
    assert result["schedule"] == "0 9 * * 1"

    # Cleanup
    await db.execute("SELECT cron.unschedule($1)", result["job_name"])
