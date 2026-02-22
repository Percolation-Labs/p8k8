"""GET /resources — list, get by id, distinct categories, rate."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.types import Resource
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository

router = APIRouter()


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
    return entity.model_dump(mode="json")


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
    return [dict(r) for r in rows]
