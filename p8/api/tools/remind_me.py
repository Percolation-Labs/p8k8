"""remind_me tool — schedule reminders via pg_cron + pg_net."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from p8.api.tools import get_db, get_encryption, get_session_id, get_user_id

_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _describe_cron(expr: str) -> str:
    """Human-readable label for common cron patterns."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return f"Custom ({expr})"
    minute, hour, dom, month, dow = parts

    time_str = ""
    if hour != "*" and minute != "*":
        time_str = f" at {int(hour):02d}:{int(minute):02d}"

    # Every day
    if dom == "*" and month == "*" and dow == "*":
        return f"Daily{time_str}"
    # Specific weekdays
    if dom == "*" and month == "*" and dow != "*":
        days = []
        for d in dow.split(","):
            try:
                days.append(_DAYS[int(d) % 7])
            except (ValueError, IndexError):
                days.append(d)
        if len(days) == 5 and all(d in days for d in _DAYS[1:6]):
            return f"Weekdays{time_str}"
        return f"Every {', '.join(days)}{time_str}"
    # Specific day of month
    if dom != "*" and month == "*" and dow == "*":
        return f"Monthly on the {_ordinal(int(dom))}{time_str}"

    return f"Custom ({expr})"


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd'][min(n % 10, 4)] if n % 10 < 4 else 'th'}"


async def remind_me(
    name: str,
    description: str,
    crontab: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Create a scheduled reminder that triggers a push notification.

    IMPORTANT — default to ONE-TIME reminders unless the user explicitly says
    "every", "daily", "weekly", "each", or similar recurring language.
    Examples:
      "remind me in the morning" → one-time, tomorrow morning (ISO datetime)
      "remind me every morning"  → recurring cron (0 7 * * *)
      "remind me on Friday"      → one-time, next Friday (ISO datetime)
      "remind me every Friday"   → recurring cron (0 9 * * 5)

    One-time reminders use an ISO datetime string (e.g. "2026-03-01T09:00:00").
    Recurring reminders use a cron expression (e.g. "0 9 * * 1" for every Monday at 9am).

    Args:
        name: Short kebab-case name for the reminder (e.g. "take-vitamins")
        description: What to remind the user about
        crontab: ISO datetime for one-time, or cron expression for recurring
        tags: Optional tags for categorization

    Returns:
        Reminder details including job name and schedule
    """
    from croniter import croniter

    user_id = get_user_id()
    if not user_id:
        return {"status": "error", "error": "user_id is required for reminders"}

    now = datetime.now(timezone.utc)
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
        frequency = f"Once — {fire_at.strftime('%b %d at %H:%M')}"
        next_fire = fire_at
    except ValueError:
        # Cron expression (recurring)
        if not croniter.is_valid(crontab):
            return {"status": "error", "error": f"Invalid crontab expression: {crontab}"}
        cron_expr = crontab
        recurrence = "recurring"
        frequency = _describe_cron(crontab)
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

    # Build the SQL that pg_cron will execute.
    # Base URL comes from the p8.internal_api_url GUC — jobs never hardcode
    # a domain, so changing the URL in postgresql.conf fixes all jobs at once.
    url_expr = "current_setting('p8.internal_api_url', true) || '/notifications/send'"
    headers_expr = (
        "jsonb_build_object("
        "'Authorization', 'Bearer ' || current_setting('p8.api_key', true), "
        "'Content-Type', 'application/json')"
    )
    if recurrence == "once":
        job_sql = (
            f"SELECT net.http_post("
            f"url := {url_expr}, "
            f"headers := {headers_expr}, "
            f"body := '{payload}'::jsonb"
            f"); "
            f"SELECT cron.unschedule('{job_name}');"
        )
    else:
        job_sql = (
            f"SELECT net.http_post("
            f"url := {url_expr}, "
            f"headers := {headers_expr}, "
            f"body := '{payload}'::jsonb"
            f");"
        )

    db = get_db()
    await db.execute(
        "SELECT cron.schedule($1, $2, $3)",
        job_name, cron_expr, job_sql,
    )

    # Persist a reminder moment — starts_timestamp is the future fire date,
    # created_at is now. Graph edges with relation="reminder" link back to
    # the source session so daily summaries can aggregate reminder counts.
    session_id = get_session_id()
    graph_edges = []
    if session_id:
        graph_edges.append({
            "target": str(session_id),
            "relation": "reminder",
            "weight": 1.0,
            "reason": f"Reminder '{name}' created in this session",
        })

    encryption = get_encryption()
    from p8.ontology.types import Moment
    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    moment = Moment(
        name=name,
        moment_type="reminder",
        category="reminder",
        summary=description,
        starts_timestamp=next_fire,
        topic_tags=tags or [],
        graph_edges=graph_edges,
        user_id=user_id,
        source_session_id=session_id,
        metadata={
            "reminder_id": str(reminder_id),
            "job_name": job_name,
            "schedule": cron_expr,
            "recurrence": recurrence,
            "frequency": frequency,
        },
    )
    [saved] = await repo.upsert(moment)

    return {
        "status": "success",
        "reminder_id": str(reminder_id),
        "moment_id": str(saved.id),
        "name": name,
        "frequency": frequency,
        "recurrence": recurrence,
    }
