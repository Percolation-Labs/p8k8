"""GET /moments — list, get, timeline, today summary, search."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
    include_future: bool = Query(False, description="Include future-dated moments (default false)"),
    db: Database = Depends(get_db),
):
    """Cursor-paginated moments feed with virtual daily summary cards.

    Returns real moments interleaved with computed daily_summary rows for each
    date with activity.  Pass ``before_date`` (the oldest event_date from the
    previous page) to fetch the next page.  Omit for the first page.

    Future-dated moments (e.g. reminders with starts_timestamp > now) are
    excluded by default.  Pass ``include_future=true`` to include them.

    Daily summaries carry a deterministic session UUID derived from
    (user_id, date) so the client can open a chat for that day.
    """
    return await db.rem_moments_feed(
        user_id=user.user_id if user else None,
        limit=limit,
        before_date=before_date,
        include_future=include_future,
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


@router.get("/reminders")
async def list_reminders(
    created_on: str | None = Query(None, description="Filter by creation date (ISO date, e.g. 2026-02-20). The date the user set the reminder."),
    due_on: str | None = Query(None, description="Filter by due/fire date (ISO date, e.g. 2026-02-23). The date the reminder is scheduled to fire."),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
):
    """List reminder moments with optional created_on / due_on filters.

    - ``created_on``: reminders the user set on this date (created_at).
      This is what the daily summary badge counts — "reminders you were
      thinking about on this day".
    - ``due_on``: reminders scheduled to fire on this date (starts_timestamp).

    Both filters can be combined. Omit both to get all reminders (limit 50).
    """
    from datetime import date as date_type

    user_id = user.user_id if user else None

    conditions = [
        "moment_type = 'reminder'",
        "deleted_at IS NULL",
        "($1::uuid IS NULL OR user_id = $1)",
    ]
    args: list = [user_id]

    if created_on:
        args.append(date_type.fromisoformat(created_on))
        conditions.append(f"(created_at AT TIME ZONE 'UTC')::date = ${len(args)}")

    if due_on:
        args.append(date_type.fromisoformat(due_on))
        conditions.append(f"(starts_timestamp AT TIME ZONE 'UTC')::date = ${len(args)}")

    where = " AND ".join(conditions)
    limit_clause = "" if (created_on or due_on) else " LIMIT 50"

    rows = await db.fetch(
        f"""SELECT id, name, summary, metadata, topic_tags,
                   starts_timestamp, created_at, graph_edges
            FROM moments
            WHERE {where}
            ORDER BY created_at DESC{limit_clause}""",
        *args,
    )

    reminders = [dict(r) for r in rows]
    return {"reminders": reminders, "count": len(reminders)}


@router.get("/search")
async def search_moments(
    q: str = Query(..., min_length=1, description="Search text"),
    limit: int = Query(10, ge=1, le=50),
    user: CurrentUser | None = Depends(get_optional_user),
    request: Request = None,  # type: ignore[assignment]
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Semantic search over moments using embeddings, with fuzzy fallback."""
    import logging
    log = logging.getLogger(__name__)

    embedding_service = request.app.state.embedding_service
    user_id = user.user_id if user else None

    # Try semantic search via embeddings, fall back to fuzzy text match
    try:
        vectors = await embedding_service.provider.embed([q])
        if vectors and vectors[0]:
            results = await db.rem_search(
                vectors[0],
                "moments",
                field="content",
                user_id=user_id,
                provider=embedding_service.provider.provider_name,
                min_similarity=0.3,
                limit=limit,
            )
            if results:
                return results
    except Exception as e:
        log.warning("Embedding search failed, falling back to fuzzy: %s", e)

    # Fuzzy fallback
    try:
        return await db.rem_fuzzy(q, user_id=user_id, limit=limit)
    except Exception as e:
        log.warning("Fuzzy search also failed: %s", e)
        return []


def _moment_response(moment: Moment) -> dict:
    """Serialize a Moment for the API, mapping image_uri → image."""
    data = moment.model_dump(mode="json")
    data["image"] = data.pop("image_uri", None)
    return data


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
    return _moment_response(moment)


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
    return [_moment_response(m) for m in moments]
