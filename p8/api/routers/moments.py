"""GET /moments — list, get, timeline, today summary, search, rate."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.types import Message, Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService

router = APIRouter()


async def _decrypt_feed_rows(
    rows: list[dict], db: Database, encryption: EncryptionService,
) -> list[dict]:
    """Decrypt platform-encrypted summaries in feed/reminder results.

    Only attempts decryption for rows with encryption_level='platform'.
    Daily summaries and unencrypted moments pass through unchanged.
    """
    platform_rows = [r for r in rows if r.get("encryption_level") == "platform"]
    if not platform_rows:
        return rows

    # Collect tenant_ids that need DEKs
    moment_ids = [r["event_id"] for r in platform_rows]
    tenant_rows = await db.fetch(
        "SELECT id, tenant_id FROM moments WHERE id = ANY($1::uuid[])",
        moment_ids,
    )
    tenant_map = {str(r["id"]): r["tenant_id"] for r in tenant_rows if r["tenant_id"]}

    # Pre-warm DEK cache for all needed tenants
    for tid in set(tenant_map.values()):
        await encryption.get_dek(tid)

    result = []
    for row in rows:
        if row.get("encryption_level") == "platform" and row.get("summary"):
            tid = tenant_map.get(str(row.get("event_id")))
            if tid:
                dec = encryption.decrypt_fields(
                    Moment, {"id": row["event_id"], "summary": row["summary"]}, tid,
                )
                row = {**row, "summary": dec["summary"]}
        result.append(row)
    return result


@router.get("/feed")
async def moments_feed(
    user: CurrentUser | None = Depends(get_optional_user),
    limit: int = Query(20, ge=1, le=100),
    before_date: str | None = Query(None, description="ISO date cursor, e.g. 2025-02-18"),
    include_future: bool = Query(False, description="Include future-dated moments (default false)"),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
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
    rows = await db.rem_moments_feed(
        user_id=user.user_id if user else None,
        limit=limit,
        before_date=before_date,
        include_future=include_future,
    )
    return await _decrypt_feed_rows(rows, db, encryption)


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
    encryption: EncryptionService = Depends(get_encryption),
):
    """Interleaved messages + moments for a session, chronologically ordered."""
    rows = await db.rem_session_timeline(session_id, limit=limit)
    if not rows:
        return rows

    # Use per-row encryption_level as primary signal (stamped at write time).
    # Fall back to tenant lookup only when encryption_level is NULL (legacy data).
    needs_decrypt = any(
        r.get("encryption_level") == "platform"
        or (r.get("encryption_level") is None and r.get("content_or_summary"))
        for r in rows
    )
    if not needs_decrypt:
        return [dict(r) for r in rows]

    # Resolve tenant_id for DEK — check messages first, fall back to moments
    tenant_row = await db.fetchrow(
        "SELECT tenant_id FROM messages WHERE session_id = $1 AND tenant_id IS NOT NULL LIMIT 1",
        session_id,
    )
    if not tenant_row:
        tenant_row = await db.fetchrow(
            "SELECT tenant_id FROM moments WHERE source_session_id = $1 AND tenant_id IS NOT NULL AND tenant_id != '' LIMIT 1",
            session_id,
        )
    tenant_id = tenant_row["tenant_id"] if tenant_row else None
    if not tenant_id:
        return [dict(r) for r in rows]

    await encryption.get_dek(tenant_id)

    # For legacy rows with no encryption_level, check tenant mode as fallback
    fallback_decrypt = await encryption.should_decrypt_on_read(tenant_id)

    result = []
    for row in rows:
        data = dict(row)
        level = data.get("encryption_level")

        # Decide per-row: decrypt platform rows, skip client/sealed/disabled/none
        should_decrypt = (
            level == "platform"
            or (level is None and fallback_decrypt)
        )

        if should_decrypt and data.get("content_or_summary"):
            if data.get("event_type") == "message":
                dec = encryption.decrypt_fields(
                    Message, {"id": data["event_id"], "content": data["content_or_summary"]}, tenant_id
                )
                data["content_or_summary"] = dec["content"]
            elif data.get("event_type") == "moment":
                dec = encryption.decrypt_fields(
                    Moment, {"id": data["event_id"], "summary": data["content_or_summary"]}, tenant_id
                )
                data["content_or_summary"] = dec["summary"]

        result.append(data)
    return result


@router.get("/reminders")
async def list_reminders(
    created_on: str | None = Query(None, description="Filter by creation date (ISO date, e.g. 2026-02-20). The date the user set the reminder."),
    due_on: str | None = Query(None, description="Filter by due/fire date (ISO date, e.g. 2026-02-23). The date the reminder is scheduled to fire."),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
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
                   starts_timestamp, created_at, graph_edges,
                   encryption_level, tenant_id
            FROM moments
            WHERE {where}
            ORDER BY created_at DESC{limit_clause}""",
        *args,
    )

    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    await repo._ensure_deks(rows)
    reminders = []
    for row in rows:
        data = dict(row)
        if data.get("encryption_level") == "platform" and data.get("tenant_id"):
            dec = encryption.decrypt_fields(Moment, data, data["tenant_id"])
            data["summary"] = dec["summary"]
        # Drop internal columns from response
        data.pop("encryption_level", None)
        data.pop("tenant_id", None)
        reminders.append(data)
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
                field="summary",
                user_id=user_id,
                provider=embedding_service.provider.provider_name,
                min_similarity=db.settings.embedding_min_similarity,
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



@router.post("/{moment_id}/rate")
async def rate_moment(
    moment_id: UUID,
    rating: int = Body(..., ge=1, le=5, embed=True),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Rate a moment (1-5)."""
    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    entity = await repo.get(moment_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Moment not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your moment")
    await db.execute(
        "UPDATE moments SET rating = $1, updated_at = NOW() WHERE id = $2",
        rating, moment_id,
    )
    entity.rating = rating
    return entity.model_dump(mode="json")


@router.delete("/reminders/{moment_id}")
async def delete_reminder(
    moment_id: UUID,
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Soft-delete a reminder moment by ID."""
    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    # Verify it exists and is a reminder owned by the user
    entity = await repo.get(moment_id)
    if not entity or entity.moment_type != "reminder":
        raise HTTPException(status_code=404, detail="Reminder not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your reminder")

    ok = await repo.delete(moment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"deleted": True, "id": str(moment_id)}


@router.delete("/{moment_id}")
async def delete_moment(
    moment_id: UUID,
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Soft-delete a moment by ID."""
    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    entity = await repo.get(moment_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Moment not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your moment")

    ok = await repo.delete(moment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Moment not found")
    return {"deleted": True, "id": str(moment_id)}


@router.get("/{moment_id}")
async def get_moment(
    moment_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Get a single moment with companion session data."""
    from p8.services.repository import Repository

    row = await db.fetchrow(
        """SELECT mo.*, s.name AS session_name, s.description AS session_description,
                  s.metadata AS session_metadata
           FROM moments mo
           LEFT JOIN sessions s ON s.id = mo.source_session_id AND s.deleted_at IS NULL
           WHERE mo.id = $1 AND mo.deleted_at IS NULL""",
        moment_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Moment not found")
    repo = Repository(Moment, db, encryption)
    await repo._ensure_deks([row])
    entity = repo._decrypt_row(row)
    # Merge session join fields onto the model dump
    result = entity.model_dump(mode="json")
    for extra in ("session_name", "session_description", "session_metadata"):
        if extra in dict(row):
            result[extra] = dict(row)[extra]
    return result


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
    """List moments with companion session data via LEFT JOIN."""
    from p8.services.repository import Repository

    user_id = user.user_id if user else None

    conditions = ["mo.deleted_at IS NULL"]
    args: list = []

    if user_id:
        args.append(user_id)
        conditions.append(f"mo.user_id = ${len(args)}")
    if session_id:
        args.append(session_id)
        conditions.append(f"mo.source_session_id = ${len(args)}")
    if moment_type:
        args.append(moment_type)
        conditions.append(f"mo.moment_type = ${len(args)}")

    args.extend([limit, offset])
    where = " AND ".join(conditions)

    rows = await db.fetch(
        f"""SELECT mo.*, s.name AS session_name, s.description AS session_description,
                   s.metadata AS session_metadata
            FROM moments mo
            LEFT JOIN sessions s ON s.id = mo.source_session_id AND s.deleted_at IS NULL
            WHERE {where}
            ORDER BY mo.created_at DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )
    repo = Repository(Moment, db, encryption)
    await repo._ensure_deks(rows)
    results = []
    for row in rows:
        entity = repo._decrypt_row(row)
        result = entity.model_dump(mode="json")
        row_dict = dict(row)
        for extra in ("session_name", "session_description", "session_metadata"):
            if extra in row_dict:
                result[extra] = row_dict[extra]
        results.append(result)
    return results
