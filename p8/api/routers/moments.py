from __future__ import annotations

"""GET /moments — list, get, timeline, today summary, search, rate."""

# TODO: move to having a moment controller and make sure CLI and routers share the same controller
#       remove imports in functions and move to top

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.types import Message, Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService
from p8.services.repository import Repository
import logging



router = APIRouter()


async def _decrypt_feed_rows(
    rows: list[dict], db: Database, encryption: EncryptionService,
) -> list[dict]:
    """Decrypt platform-encrypted summaries in feed results.

    Uses the Repository decrypt path (same as list_moments, get_moment, etc.)
    to ensure consistent DEK resolution and AAD construction.  Daily summaries
    and unencrypted moments pass through unchanged.
    """
    from p8.services.repository import Repository

    # Identify feed rows that may need decryption
    candidate_ids = [
        r["event_id"] for r in rows
        if r.get("summary") and (
            r.get("encryption_level") == "platform"
            or r.get("encryption_level") is None
        )
    ]
    if not candidate_ids:
        return rows

    # Batch-fetch full moment rows and decrypt through Repository
    repo = Repository(Moment, db, encryption)
    full_rows = await db.fetch(
        "SELECT * FROM moments WHERE id = ANY($1::uuid[])",
        candidate_ids,
    )
    await repo._ensure_deks(full_rows)

    decrypted: dict = {}
    for frow in full_rows:
        entity = repo._decrypt_row(frow)
        decrypted[entity.id] = entity.summary

    # Replace summaries in feed rows with decrypted text
    result = []
    for row in rows:
        eid = row.get("event_id")
        if eid in decrypted and decrypted[eid] is not None:
            row = {**row, "summary": decrypted[eid]}
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
    """Virtual 'today' moment with deterministic session_id.

    Always returns a result (even with no activity) so the client can
    obtain today's session UUID before the first chat message.
    """
    memory = MemoryService(db, encryption)
    return await memory.build_today_summary(user_id=user.user_id if user else None)


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
        return []

    # Use per-row encryption_level as primary signal (stamped at write time).
    needs_decrypt = any(
        r.get("encryption_level") == "platform"
        or (r.get("encryption_level") is None and r.get("content_or_summary"))
        for r in rows
    )
    if not needs_decrypt:
        return [dict(r) for r in rows]



    msg_ids = [
        r["event_id"] for r in rows
        if r.get("event_type") == "message"
        and r.get("content_or_summary")
        and (r.get("encryption_level") == "platform" or r.get("encryption_level") is None)
    ]
    mom_ids = [
        r["event_id"] for r in rows
        if r.get("event_type") == "moment"
        and r.get("content_or_summary")
        and (r.get("encryption_level") == "platform" or r.get("encryption_level") is None)
    ]

    decrypted_content: dict = {}

    if msg_ids:
        msg_repo = Repository(Message, db, encryption)
        msg_rows = await db.fetch(
            "SELECT * FROM messages WHERE id = ANY($1::uuid[])", msg_ids,
        )
        await msg_repo._ensure_deks(msg_rows)
        for mrow in msg_rows:
            entity = msg_repo._decrypt_row(mrow)
            decrypted_content[entity.id] = entity.content

    if mom_ids:
        mom_repo = Repository(Moment, db, encryption)
        mom_rows = await db.fetch(
            "SELECT * FROM moments WHERE id = ANY($1::uuid[])", mom_ids,
        )
        await mom_repo._ensure_deks(mom_rows)
        for mrow in mom_rows:
            moment = mom_repo._decrypt_row(mrow)
            decrypted_content[moment.id] = moment.summary  # type: ignore[union-attr]

    timeline = []
    for row in rows:
        data = dict(row)
        eid = data.get("event_id")
        if eid in decrypted_content and decrypted_content[eid] is not None:
            data["content_or_summary"] = decrypted_content[eid]
        timeline.append(data)
    return timeline


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
    """Semantic search over moments using embeddings, with tag match and fuzzy fallback.

    Returns flat moment objects (same shape as GET /moments/) so clients can
    render them with the same card widgets used on the feed.
    """

    log = logging.getLogger(__name__)

    embedding_service = request.app.state.embedding_service
    user_id = user.user_id if user else None


    _DROP_COLS = {"encryption_level", "tenant_id", "user_id", "deleted_at", "graph_edges"}

    async def _decrypt_and_dump(raw_rows) -> list[dict]:
        """Decrypt moment rows via Repository and return JSON-safe dicts."""
        repo = Repository(Moment, db, encryption)
        await repo._ensure_deks(raw_rows)
        out = []
        for row in raw_rows:
            entity = repo._decrypt_row(row)
            d = entity.model_dump(mode="json")
            for col in _DROP_COLS:
                d.pop(col, None)
            out.append(d)
        return out

    def _unwrap_rem(rows: list[dict]) -> list[dict]:
        """Extract IDs from REM result wrappers for re-fetch."""
        ids = []
        for row in rows:
            d = row.get("data") or row
            if isinstance(d, dict) and d.get("id"):
                ids.append(d["id"])
        return ids

    # 1. Tag match — if the query looks like tags, try direct topic_tags overlap
    assert db.pool is not None
    tag_results = await db.pool.fetch(
        "SELECT * FROM moments "
        "WHERE deleted_at IS NULL "
        "  AND ($1::uuid IS NULL OR user_id IS NULL OR user_id = $1) "
        "  AND topic_tags && string_to_array($2, ',')::text[] "
        "ORDER BY starts_timestamp DESC NULLS LAST "
        "LIMIT $3",
        user_id, q.lower().strip(), limit,
    )
    if tag_results:
        return await _decrypt_and_dump(tag_results)

    # 2. Semantic search via embeddings
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
                ids = _unwrap_rem(results)
                if ids:
                    rows = await db.pool.fetch(
                        "SELECT * FROM moments WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL "
                        "ORDER BY starts_timestamp DESC NULLS LAST",
                        ids,
                    )
                    if rows:
                        return await _decrypt_and_dump(rows)
    except Exception as e:
        log.warning("Embedding search failed, falling back to fuzzy: %s", e)

    # 3. Fuzzy text fallback
    try:
        fuzzy = await db.rem_fuzzy(q, user_id=user_id, limit=limit)
        if fuzzy:
            ids = _unwrap_rem(fuzzy)
            if ids:
                rows = await db.pool.fetch(
                    "SELECT * FROM moments WHERE id = ANY($1::uuid[]) AND deleted_at IS NULL "
                    "ORDER BY starts_timestamp DESC NULLS LAST",
                    ids,
                )
                if rows:
                    return await _decrypt_and_dump(rows)
    except Exception as e:
        log.warning("Fuzzy search also failed: %s", e)

    return []



@router.get("/by-name/{name}")
async def get_moment_by_name(
    name: str,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Resolve a moment by its entity key (name).

    Internal link shorthand: clients use `moment://entity-key` URIs
    (e.g. in dream summaries) which resolve to this endpoint.
    The Flutter app parses these URIs and calls GET /moments/by-name/{key}
    to fetch the moment, then navigates to the detail card.
    """
    # Try as UUID first (moment IDs are unique and preferred for links)
    moment_id = None
    try:
        moment_id = UUID(name)
    except (ValueError, AttributeError):
        pass

    if moment_id is None:
        # Fall back to kv_store entity_key lookup
        row = await db.fetchrow(
            "SELECT entity_id FROM kv_store WHERE entity_key = $1 AND entity_type = 'moments'",
            name,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Moment not found")
        moment_id = row["entity_id"]

    # Delegate to the same logic as GET /moments/{id}
    moment_row = await db.fetchrow(
        """SELECT mo.*, s.name AS session_name, s.description AS session_description,
                  s.metadata AS session_metadata
           FROM moments mo
           LEFT JOIN sessions s ON s.id = mo.source_session_id AND s.deleted_at IS NULL
           WHERE mo.id = $1 AND mo.deleted_at IS NULL""",
        moment_id,
    )
    if not moment_row:
        raise HTTPException(status_code=404, detail="Moment not found")
    repo = Repository(Moment, db, encryption)
    await repo._ensure_deks([moment_row])
    entity = repo._decrypt_row(moment_row)
    result = entity.model_dump(mode="json")
    for extra in ("session_name", "session_description", "session_metadata"):
        if extra in dict(moment_row):
            result[extra] = dict(moment_row)[extra]
    return result


_EDITABLE_MOMENT_TYPES = {"note", "content_upload", "voice_note"}


@router.patch("/{moment_id}")
async def update_moment(
    moment_id: UUID,
    body: dict = Body(...),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Update a text note moment (summary, name).

    Only moments with editable types (note, content_upload, voice_note) can
    be updated.  Changing the summary triggers automatic re-embedding via
    the database trigger.
    """
    repo = Repository(Moment, db, encryption)
    entity = await repo.get(moment_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Moment not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your moment")
    if entity.moment_type not in _EDITABLE_MOMENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Only {', '.join(sorted(_EDITABLE_MOMENT_TYPES))} moments can be edited",
        )

    allowed = {"summary", "name"}
    updates = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    set_clauses = []
    args: list = []
    for key, val in updates.items():
        args.append(val)
        set_clauses.append(f"{key} = ${len(args)}")
    args.append(moment_id)
    set_sql = ", ".join(set_clauses)

    await db.execute(
        f"UPDATE moments SET {set_sql}, updated_at = NOW() WHERE id = ${len(args)}",
        *args,
    )

    # Return refreshed entity
    updated = await repo.get(moment_id)
    return updated.model_dump(mode="json") if updated else {"updated": True}


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
    cancel_cron: bool = Query(False, description="Also unschedule the pg_cron job"),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Soft-delete a reminder moment by ID.

    By default only hides the reminder from the feed (soft-delete).
    Pass ``cancel_cron=true`` to also unschedule the pg_cron job so
    the reminder never fires again.
    """
    from p8.services.repository import Repository

    repo = Repository(Moment, db, encryption)
    # Verify it exists and is a reminder owned by the user
    entity = await repo.get(moment_id)
    if not entity or entity.moment_type != "reminder":
        raise HTTPException(status_code=404, detail="Reminder not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your reminder")

    cron_cancelled = False
    if cancel_cron:
        job_name = (entity.metadata or {}).get("job_name")
        if job_name:
            try:
                await db.execute("SELECT cron.unschedule($1)", job_name)
                cron_cancelled = True
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to unschedule cron job %s for reminder %s",
                    job_name, moment_id,
                )

    ok = await repo.delete(moment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"deleted": True, "id": str(moment_id), "cron_cancelled": cron_cancelled}


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
