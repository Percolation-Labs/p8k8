"""POST/GET/DELETE /share — share moments with other users via graph_edges."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from p8.api.deps import get_db, get_encryption
from p8.ontology.types import Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository

router = APIRouter()


class ShareTarget(BaseModel):
    """Identifies a moment + target user for share/unshare operations."""

    moment_id: str
    target_user_id: UUID


def _share_key(user_id: UUID) -> str:
    return f"user:{user_id}"


async def _get_moment_or_404(
    moment_id: str, db: Database, encryption: EncryptionService,
) -> Moment:
    repo = Repository(Moment, db, encryption)
    moment = await repo.get(UUID(moment_id))
    if not moment:
        raise HTTPException(404, "Moment not found")
    return moment


@router.post("/")
async def share_moment(
    body: ShareTarget,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Share a moment with another user by adding a graph_edge."""
    moment = await _get_moment_or_404(body.moment_id, db, encryption)
    target_key = _share_key(body.target_user_id)

    if any(
        e.get("target") == target_key and e.get("relation") == "shared_with"
        for e in moment.graph_edges
    ):
        return {"status": "already_shared", "moment_id": body.moment_id}

    moment.graph_edges = [
        *moment.graph_edges,
        {"target": target_key, "relation": "shared_with", "weight": 1.0},
    ]
    repo = Repository(Moment, db, encryption)
    await repo.upsert(moment)
    return {"status": "shared", "moment_id": body.moment_id, "target_user_id": body.target_user_id}


@router.delete("/")
async def unshare_moment(
    body: ShareTarget,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Remove a share edge from a moment."""
    moment = await _get_moment_or_404(body.moment_id, db, encryption)
    target_key = _share_key(body.target_user_id)

    moment.graph_edges = [
        e for e in moment.graph_edges
        if not (e.get("target") == target_key and e.get("relation") == "shared_with")
    ]
    repo = Repository(Moment, db, encryption)
    await repo.upsert(moment)
    return {"status": "unshared", "moment_id": body.moment_id}


@router.get("/moment/{moment_id}")
async def get_moment_shares(
    moment_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """List users a moment is shared with."""
    moment = await _get_moment_or_404(str(moment_id), db, encryption)
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
    # Build the JSONB containment filter as a properly serialized parameter
    filter_json = json.dumps([{"target": _share_key(user_id), "relation": "shared_with"}])
    rows = await db.fetch(
        "SELECT * FROM moments"
        " WHERE deleted_at IS NULL AND graph_edges @> $1::jsonb"
        " ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        filter_json, limit, offset,
    )
    repo = Repository(Moment, db, encryption)
    return [Moment(**dict(row)).model_dump(mode="json") for row in rows]
