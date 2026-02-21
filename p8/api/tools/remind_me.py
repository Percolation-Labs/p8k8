"""remind_me tool — schedule reminders via pg_cron + pg_net."""

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

    Each reminder becomes a pg_cron job that calls /notifications/send directly.
    Moments are only created when the notification actually fires.

    Args:
        name: Short name for the reminder (e.g. "take-vitamins")
        description: What to remind the user about
        crontab: Cron expression for recurring, or ISO datetime for one-time
        tags: Optional tags for categorization
        user_id: User to send the reminder to

    Returns:
        Reminder details including job name and schedule
    """
    from croniter import croniter
    from p8.settings import Settings

    if not user_id:
        return {"status": "error", "error": "user_id is required for reminders"}

    now = datetime.now(timezone.utc)
    settings = Settings()
    api_url = f"{settings.api_base_url}/notifications/send"
    reminder_id = uuid4()
    job_name = f"reminder-{reminder_id}"

    # Determine recurrence and build cron expression
    try:
        # Try ISO datetime first (one-time)
        fire_at = datetime.fromisoformat(crontab)
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        # Convert to cron: minute hour day month *
        cron_expr = f"{fire_at.minute} {fire_at.hour} {fire_at.day} {fire_at.month} *"
        recurrence = "once"
        next_fire = fire_at
    except ValueError:
        # Cron expression (recurring)
        if not croniter.is_valid(crontab):
            return {"status": "error", "error": f"Invalid crontab expression: {crontab}"}
        cron_expr = crontab
        recurrence = "recurring"
        cron = croniter(crontab, now)
        next_fire = cron.get_next(datetime)
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=timezone.utc)

    # Build the payload for /notifications/send
    payload = json.dumps({
        "user_ids": [str(user_id)],
        "title": name,
        "body": description,
        "data": {"reminder_id": str(reminder_id), "tags": tags or []},
    })

    # Build the SQL that pg_cron will execute
    # For one-time: send + unschedule in one shot
    if recurrence == "once":
        job_sql = (
            f"SELECT net.http_post("
            f"url := '{api_url}', "
            f"headers := '{{\"Content-Type\": \"application/json\"}}'::jsonb, "
            f"body := '{payload}'::jsonb"
            f"); "
            f"SELECT cron.unschedule('{job_name}');"
        )
    else:
        job_sql = (
            f"SELECT net.http_post("
            f"url := '{api_url}', "
            f"headers := '{{\"Content-Type\": \"application/json\"}}'::jsonb, "
            f"body := '{payload}'::jsonb"
            f");"
        )

    db = get_db()
    await db.execute(
        "SELECT cron.schedule($1, $2, $3)",
        job_name, cron_expr, job_sql,
    )

    return {
        "status": "success",
        "reminder_id": str(reminder_id),
        "job_name": job_name,
        "name": name,
        "schedule": cron_expr,
        "next_fire": next_fire.isoformat(),
        "recurrence": recurrence,
    }
