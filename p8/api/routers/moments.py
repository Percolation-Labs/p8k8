"""GET /moments — list, get, timeline, today summary."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.types import Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService
from p8.services.repository import Repository

router = APIRouter()


@router.get("/feed")
async def moments_feed(
    user: CurrentUser | None = Depends(get_optional_user),
    limit: int = Query(20, ge=1, le=100),
    before_date: str | None = Query(None, description="ISO date cursor, e.g. 2025-02-18"),
    db: Database = Depends(get_db),
):
    """Cursor-paginated moments feed with virtual daily summary cards.

    Returns real moments interleaved with computed daily_summary rows for each
    date with activity.  Pass ``before_date`` (the oldest event_date from the
    previous page) to fetch the next page.  Omit for the first page.

    Daily summaries carry a deterministic session UUID derived from
    (user_id, date) so the client can open a chat for that day.
    """
    return await db.rem_moments_feed(
        user_id=user.user_id if user else None,
        limit=limit,
        before_date=before_date,
    )


@router.get("/today")
async def today_summary(
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Virtual 'today' moment — delegates to rem_moments_feed for today's daily_summary."""
    memory = MemoryService(db, encryption)
    result = await memory.build_today_summary(user_id=user.user_id if user else None)
    if result is None:
        return {"detail": "No activity today"}
    return result


@router.get("/session/{session_id}")
async def session_timeline(
    session_id: UUID,
    limit: int = Query(50, ge=1, le=500),
    db: Database = Depends(get_db),
):
    """Interleaved messages + moments for a session, chronologically ordered."""
    rows = await db.rem_session_timeline(session_id, limit=limit)
    return rows


@router.get("/{moment_id}")
async def get_moment(
    moment_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Get a single moment by ID."""
    repo = Repository(Moment, db, encryption)
    moment = await repo.get(moment_id)
    if not moment:
        raise HTTPException(status_code=404, detail="Moment not found")
    return moment


@router.get("/")
async def list_moments(
    user: CurrentUser | None = Depends(get_optional_user),
    session_id: UUID | None = Query(None),
    moment_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List moments with optional filters. User from JWT or x-user-id header."""
    repo = Repository(Moment, db, encryption)
    filters = {}
    if session_id:
        filters["source_session_id"] = str(session_id)
    if moment_type:
        filters["moment_type"] = moment_type
    moments = await repo.find(
        user_id=user.user_id if user else None, filters=filters, limit=limit, offset=offset
    )
    return moments
