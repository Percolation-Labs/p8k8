"""Notification relay — send push notifications to users."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


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


@router.post("/send")
async def send_notification(request: Request, body: SendRequest):
    """Send a push notification to one or more users by ID.

    Reads device tokens from each user's `devices` JSONB field.
    Creates a notification moment in the user's feed automatically.
    Called directly by pg_cron jobs (reminders, digests) via pg_net.
    """
    svc = _get_service(request)
    all_results = []
    for uid in body.user_ids:
        results = await svc.send_to_user(uid, body.title, body.body, body.data)
        all_results.extend(results)
    return {"results": all_results}
