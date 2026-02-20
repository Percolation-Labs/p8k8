"""POST/GET/DELETE /share — share moments with other users via graph_edges."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from p8.api.deps import get_db, get_encryption
from p8.ontology.types import Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository

router = APIRouter()


class ShareRequest(BaseModel):
    moment_id: str
    target_user_id: UUID


class UnshareRequest(BaseModel):
    moment_id: str
    target_user_id: UUID


@router.post("/")
async def share_moment(
    body: ShareRequest,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Share a moment with another user by adding a graph_edge."""
    repo = Repository(Moment, db, encryption)
    moment = await repo.get(UUID(body.moment_id))
    if not moment:
        raise HTTPException(status_code=404, detail="Moment not found")

    # Check if already shared
    target_key = f"user:{body.target_user_id}"
    for edge in moment.graph_edges:
        if edge.get("target") == target_key and edge.get("relation") == "shared_with":
            return {"status": "already_shared", "moment_id": body.moment_id}

    # Add share edge
    edges = list(moment.graph_edges)
    edges.append({"target": target_key, "relation": "shared_with", "weight": 1.0})
    moment.graph_edges = edges

    await repo.upsert(moment)
    return {"status": "shared", "moment_id": body.moment_id, "target_user_id": body.target_user_id}


@router.delete("/")
async def unshare_moment(
    body: UnshareRequest,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Remove a share edge from a moment."""
    repo = Repository(Moment, db, encryption)
    moment = await repo.get(UUID(body.moment_id))
    if not moment:
        raise HTTPException(status_code=404, detail="Moment not found")

    target_key = f"user:{body.target_user_id}"
    edges = [
        e for e in moment.graph_edges
        if not (e.get("target") == target_key and e.get("relation") == "shared_with")
    ]
    moment.graph_edges = edges
    await repo.upsert(moment)
    return {"status": "unshared", "moment_id": body.moment_id}


@router.get("/moment/{moment_id}")
async def get_moment_shares(
    moment_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List users a moment is shared with."""
    repo = Repository(Moment, db, encryption)
    moment = await repo.get(moment_id)
    if not moment:
        raise HTTPException(status_code=404, detail="Moment not found")

    shared_with = [
        e.get("target", "").replace("user:", "")
        for e in moment.graph_edges
        if e.get("relation") == "shared_with"
    ]
    return {"moment_id": str(moment_id), "shared_with": shared_with}


@router.get("/with-me")
async def shared_with_me(
    user_id: UUID = Query(...),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List moments shared with the given user via graph_edges traversal."""
    target_key = f"user:{user_id}"
    rows = await db.pool.fetch(
        """
        SELECT * FROM moments
        WHERE deleted_at IS NULL
          AND graph_edges @> $1::jsonb
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
        """,
        f'[{{"target": "{target_key}", "relation": "shared_with"}}]',
        limit,
        offset,
    )
    repo = Repository(Moment, db, encryption)
    moments = []
    for row in rows:
        m = Moment(**dict(row))
        moments.append(m.model_dump(mode="json"))
    return moments
