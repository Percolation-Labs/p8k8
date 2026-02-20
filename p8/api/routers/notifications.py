"""Notification relay — send push notifications to users."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class SendRequest(BaseModel):
    user_ids: list[UUID]
    title: str
    body: str
    data: dict | None = None


def _get_service(request: Request):
    svc = getattr(request.app.state, "notification_service", None)
    if svc is None:
        raise HTTPException(503, "Notification service not configured")
    return svc


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Database not configured")
    return db


@router.post("/send")
async def send_notification(request: Request, body: SendRequest):
    """Send a push notification to one or more users by ID.

    Reads device tokens from each user's `devices` JSONB field.
    Primary target for pg_cron + pg_net scheduled sends.
    """
    svc = _get_service(request)
    all_results = []
    for uid in body.user_ids:
        results = await svc.send_to_user(uid, body.title, body.body, body.data)
        all_results.extend(results)
    return {"results": all_results}


@router.post("/process-reminders")
async def process_reminders(request: Request):
    """Process due reminders — called by pg_cron every minute.

    Queries reminder moments where starts_timestamp <= NOW() and
    ends_timestamp IS NULL (not yet completed/fired). For each:
    - Sends a push notification to the user
    - Recurring: computes next fire time via croniter, updates starts_timestamp
    - One-time: sets ends_timestamp = NOW() to mark completed
    """
    svc = _get_service(request)
    db = _get_db(request)

    rows = await db.fetch(
        """
        SELECT id, name, summary, user_id, metadata
        FROM moments
        WHERE moment_type = 'reminder'
          AND starts_timestamp <= NOW()
          AND ends_timestamp IS NULL
          AND deleted_at IS NULL
        """
    )

    processed = 0
    for row in rows:
        reminder_id = row["id"]
        user_id = row["user_id"]
        name = row["name"]
        summary = row["summary"]
        metadata = row["metadata"] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        # Send notification
        if user_id:
            try:
                await svc.send_to_user(
                    user_id, name, summary or "",
                    {"reminder_id": str(reminder_id)},
                )
            except Exception as e:
                logger.error("Failed to send reminder %s: %s", reminder_id, e)
                continue

        recurrence = metadata.get("recurrence", "once")
        crontab = metadata.get("crontab")

        if recurrence == "recurring" and crontab:
            # Compute next fire time
            from croniter import croniter
            now = datetime.now(timezone.utc)
            cron = croniter(crontab, now)
            next_fire = cron.get_next(datetime)
            if next_fire.tzinfo is None:
                next_fire = next_fire.replace(tzinfo=timezone.utc)
            await db.execute(
                "UPDATE moments SET starts_timestamp = $1, updated_at = NOW() WHERE id = $2",
                next_fire, reminder_id,
            )
        else:
            # One-time: mark completed
            await db.execute(
                "UPDATE moments SET ends_timestamp = NOW(), updated_at = NOW() WHERE id = $1",
                reminder_id,
            )

        processed += 1

    return {"processed": processed}
