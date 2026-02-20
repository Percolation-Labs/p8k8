"""remind_me tool — create scheduled reminders as moments."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from p8.api.tools import get_db


async def remind_me(
    name: str,
    description: str,
    crontab: str,
    tags: list[str] | None = None,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Create a scheduled reminder that triggers a push notification.

    One-time reminders use an ISO datetime string (e.g. "2025-03-01T09:00:00").
    Recurring reminders use a cron expression (e.g. "0 9 * * 1" for every Monday at 9am).

    Args:
        name: Short name for the reminder (e.g. "take-vitamins")
        description: What to remind the user about
        crontab: Cron expression for recurring, or ISO datetime for one-time
        tags: Optional tags for categorization
        user_id: User to send the reminder to

    Returns:
        Reminder details including ID and next fire time
    """
    from croniter import croniter

    now = datetime.now(timezone.utc)

    # Determine recurrence type and compute next fire time
    is_recurring = False
    try:
        # Try parsing as ISO datetime first (one-time)
        next_fire = datetime.fromisoformat(crontab)
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=timezone.utc)
        recurrence = "once"
    except ValueError:
        # Must be a cron expression (recurring)
        if not croniter.is_valid(crontab):
            return {"status": "error", "error": f"Invalid crontab expression: {crontab}"}
        is_recurring = True
        cron = croniter(crontab, now)
        next_fire = cron.get_next(datetime)
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=timezone.utc)
        recurrence = "recurring"

    reminder_id = uuid4()
    metadata = {"crontab": crontab, "recurrence": recurrence}

    db = get_db()
    await db.execute(
        """
        INSERT INTO moments (id, name, moment_type, summary, starts_timestamp,
                             user_id, tags, metadata)
        VALUES ($1, $2, 'reminder', $3, $4, $5, $6, $7)
        """,
        reminder_id,
        name,
        description,
        next_fire,
        user_id,
        tags or [],
        json.dumps(metadata),
    )

    return {
        "status": "success",
        "reminder_id": str(reminder_id),
        "name": name,
        "next_fire": next_fire.isoformat(),
        "recurrence": recurrence,
    }
