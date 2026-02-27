"""GET /resources — list, get by id, distinct categories, rate, reading."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as date_type, datetime, timezone
from enum import Enum
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.base import deterministic_id
from p8.ontology.types import Moment, Resource
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.graph import merge_graph_edges
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.parsing import ensure_parsed

log = logging.getLogger(__name__)

router = APIRouter()


def _add_source_url(d: dict) -> dict:
    """Attach ``source_url`` — a link to the original document if available."""
    meta = dict(ensure_parsed(d.get("metadata"), default={}) or {})
    file_id = meta.get("file_id")
    if file_id:
        d["source_url"] = f"/content/files/{file_id}"
    elif (d.get("uri") or "").startswith("http"):
        d["source_url"] = d["uri"]
    else:
        d["source_url"] = None
    return d


async def _update_reading_mosaic(
    items: list[dict], moment_id: UUID, db: Database
) -> None:
    """Generate a mosaic thumbnail from reading items and update the moment.

    Fire-and-forget — failures are logged but never surface to the caller.
    """
    try:
        from p8.services.content import generate_mosaic_thumbnail

        image_uris = [i.get("image_uri", "") for i in items]
        data_uri = await generate_mosaic_thumbnail(image_uris)
        if data_uri:
            await db.execute(
                "UPDATE moments SET image_uri = $1, updated_at = NOW() WHERE id = $2",
                data_uri,
                moment_id,
            )
    except Exception:
        log.warning("Reading mosaic generation failed", exc_info=True)


# ---------------------------------------------------------------------------
# Reading models
# ---------------------------------------------------------------------------


class ReadingAction(str, Enum):
    click = "click"
    bookmark = "bookmark"
    unbookmark = "unbookmark"


class ReadingActionBody(BaseModel):
    action: ReadingAction


# ---------------------------------------------------------------------------
# Reading endpoints (before /{resource_id} to avoid path conflict)
# ---------------------------------------------------------------------------


@router.get("/reading")
async def get_reading(
    date: str | None = Query(None, description="ISO date filter, e.g. 2026-02-22"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List reading moments for the current user, newest first."""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if date:
        target_date = date_type.fromisoformat(date)
        rows = await db.fetch(
            """SELECT * FROM moments
               WHERE moment_type = 'reading' AND user_id = $1
                 AND deleted_at IS NULL
                 AND (created_at AT TIME ZONE 'UTC')::date = $2
               ORDER BY created_at DESC
               LIMIT $3 OFFSET $4""",
            user.user_id, target_date, limit, offset,
        )
    else:
        rows = await db.fetch(
            """SELECT * FROM moments
               WHERE moment_type = 'reading' AND user_id = $1
                 AND deleted_at IS NULL
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            user.user_id, limit, offset,
        )

    return [dict(r) for r in rows]


@router.post("/{resource_id}/reading")
async def record_reading(
    resource_id: UUID,
    body: ReadingActionBody,
    user: CurrentUser = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Record a click or bookmark on a resource, upserting the daily reading moment."""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Validate the resource exists
    resource_repo = Repository(Resource, db, encryption)
    resource = await resource_repo.get(resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    uid = str(user.user_id)

    # Handle unbookmark — just remove from resource metadata, no reading moment update
    if body.action == ReadingAction.unbookmark:
        meta = dict(ensure_parsed(resource.metadata, default={}) or {})
        bookmarked_by: list = meta.get("bookmarked_by", [])
        if uid in bookmarked_by:
            bookmarked_by.remove(uid)
            meta["bookmarked_by"] = bookmarked_by
            await db.execute(
                "UPDATE resources SET metadata = $1::jsonb, updated_at = NOW() WHERE id = $2",
                json.dumps(meta), resource_id,
            )
        return {"resource_id": str(resource_id), "bookmarked": False}

    # Bookmark action — persist on the resource so list_resources can show state
    if body.action == ReadingAction.bookmark:
        meta_bk = dict(ensure_parsed(resource.metadata, default={}) or {})
        bookmarked_by_bk: list = meta_bk.get("bookmarked_by", [])
        if uid not in bookmarked_by_bk:
            bookmarked_by_bk.append(uid)
            meta_bk["bookmarked_by"] = bookmarked_by_bk
            await db.execute(
                "UPDATE resources SET metadata = $1::jsonb, updated_at = NOW() WHERE id = $2",
                json.dumps(meta_bk), resource_id,
            )

    today = datetime.now(timezone.utc).date()
    moment_name = f"reading-{today.isoformat()}"
    moment_id = deterministic_id("moments", moment_name, user.user_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Build the new item
    new_item = {
        "resource_id": str(resource_id),
        "uri": resource.uri or "",
        "title": resource.name or "",
        "image_uri": resource.image_uri or "",
        "tags": resource.tags or [],
        "action": body.action.value,
        "timestamp": now_iso,
    }

    new_edge = {
        "target": resource.name,
        "relation": "read",
        "weight": 1.0,
    }

    # Check if the moment already exists
    existing = await db.fetchrow(
        "SELECT id, metadata, graph_edges FROM moments WHERE id = $1 AND deleted_at IS NULL",
        moment_id,
    )

    if existing:
        # Parse existing metadata
        meta_ex = dict(ensure_parsed(existing["metadata"], default={}) or {})
        items: list = meta_ex.get("items", [])

        # Deduplicate by resource_id
        already = any(i.get("resource_id") == str(resource_id) for i in items)
        if already:
            return {
                "moment_id": str(moment_id),
                "action": body.action.value,
                "duplicate": True,
            }

        # Append new item and update metadata
        items.append(new_item)
        meta_ex["items"] = items
        meta_ex["resource_count"] = len(items)

        # Merge graph edges
        existing_edges = ensure_parsed(existing["graph_edges"], default=[])
        if not isinstance(existing_edges, list):
            existing_edges = []
        merged_edges = merge_graph_edges(existing_edges, [new_edge])

        # Collect topic_tags from all items
        all_tags: list[str] = []
        for item in items:
            all_tags.extend(item.get("tags", []))
        unique_tags = list(dict.fromkeys(all_tags))

        await db.execute(
            """UPDATE moments
               SET metadata = $1::jsonb,
                   graph_edges = $2::jsonb,
                   topic_tags = $3,
                   updated_at = NOW()
               WHERE id = $4""",
            json.dumps(meta_ex),
            json.dumps(merged_edges),
            unique_tags,
            moment_id,
        )

        # Rebuild mosaic from all items (fire-and-forget)
        asyncio.create_task(_update_reading_mosaic(items, moment_id, db))

        return {
            "moment_id": str(moment_id),
            "action": body.action.value,
            "duplicate": False,
            "item_count": len(items),
        }
    else:
        # First interaction of the day — create moment + companion session
        memory = MemoryService(db, encryption)
        meta = {
            "source": "reading_tracker",
            "resource_count": 1,
            "items": [new_item],
        }

        session_id = deterministic_id("sessions", moment_name, user.user_id)
        moment, session = await memory.create_moment_session(
            name=moment_name,
            moment_type="reading",
            summary="",
            metadata=meta,
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            topic_tags=resource.tags or [],
            graph_edges=[new_edge],
        )

        # Generate mosaic from first item (fire-and-forget)
        asyncio.create_task(
            _update_reading_mosaic([new_item], moment.id, db)
        )

        return {
            "moment_id": str(moment.id),
            "session_id": str(session.id),
            "action": body.action.value,
            "duplicate": False,
            "created": True,
        }


@router.get("/categories")
async def resource_categories(
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
):
    """Distinct resource categories with counts (user-scoped)."""
    user_id = user.user_id if user else None
    rows = await db.fetch(
        """SELECT category, COUNT(*) AS count
           FROM resources
           WHERE deleted_at IS NULL AND category IS NOT NULL
             AND ($1::uuid IS NULL OR user_id = $1)
           GROUP BY category
           ORDER BY count DESC""",
        user_id,
    )
    return [dict(r) for r in rows]


@router.post("/{resource_id}/rate")
async def rate_resource(
    resource_id: UUID,
    rating: int = Body(..., ge=1, le=5, embed=True),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Rate a resource (1-5)."""
    repo = Repository(Resource, db, encryption)
    entity = await repo.get(resource_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Resource not found")
    if user and entity.user_id and entity.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not your resource")
    await db.execute(
        "UPDATE resources SET rating = $1, updated_at = NOW() WHERE id = $2",
        rating, resource_id,
    )
    entity.rating = rating
    return entity.model_dump(mode="json")


@router.get("/by-name/{name}")
async def get_resource_by_name(
    name: str,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Resolve a resource by its entity key (name).

    Internal link shorthand: clients use `resource://entity-key` URIs
    (e.g. in dream summaries) which resolve to this endpoint.
    The Flutter app parses these URIs and calls GET /resources/by-name/{key}
    to fetch the resource, then displays it in a detail sheet.
    """
    # Try as UUID first (resource IDs are unique and preferred for links)
    resource_id = None
    try:
        resource_id = UUID(name)
    except (ValueError, AttributeError):
        pass

    if resource_id is None:
        # Fall back to kv_store entity_key lookup
        row = await db.fetchrow(
            "SELECT entity_id FROM kv_store WHERE entity_key = $1 AND entity_type = 'resources'",
            name,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Resource not found")
        resource_id = row["entity_id"]

    repo = Repository(Resource, db, encryption)
    entity = await repo.get(resource_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Resource not found")
    return _add_source_url(entity.model_dump(mode="json"))


@router.get("/{resource_id}")
async def get_resource(
    resource_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Get a single resource by ID."""
    repo = Repository(Resource, db, encryption)
    entity = await repo.get(resource_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Resource not found")
    return _add_source_url(entity.model_dump(mode="json"))


@router.get("/")
async def list_resources(
    category: str | None = Query(None),
    date: str | None = Query(None, description="Filter by creation date (ISO date, e.g. 2026-02-20)"),
    tags: str | None = Query(None, description="Comma-separated tags (all must match)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List resources with optional category, date, and tag filters."""
    from datetime import date as date_type

    user_id = user.user_id if user else None

    conditions = ["deleted_at IS NULL"]
    args: list = []
    idx = 1

    if user_id:
        conditions.append(f"user_id = ${idx}")
        args.append(user_id)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        args.append(category)
        idx += 1
    if date:
        conditions.append(f"(created_at AT TIME ZONE 'UTC')::date = ${idx}")
        args.append(date_type.fromisoformat(date))
        idx += 1
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            conditions.append(f"tags @> ${idx}")
            args.append(tag_list)
            idx += 1

    where = " AND ".join(conditions)
    args.extend([limit, offset])

    rows = await db.fetch(
        f"""SELECT * FROM resources
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )
    uid = str(user_id) if user_id else None
    results = []
    for r in rows:
        d = dict(r)
        res_meta = dict(ensure_parsed(d.get("metadata"), default={}) or {})
        d["bookmarked"] = uid in (res_meta.get("bookmarked_by") or []) if uid else False
        _add_source_url(d)
        results.append(d)
    return results
