"""POST /query — execute REM queries (structured and raw dialect)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from p8.api.deps import get_db
from p8.services.database import Database

router = APIRouter()


class QueryRequest(BaseModel):
    mode: str  # LOOKUP | SEARCH | FUZZY | TRAVERSE | SQL
    key: str | None = None
    query: str | None = None
    table: str | None = None
    field: str = "content"
    embedding: list[float] | None = None
    tenant_id: str | None = None
    user_id: UUID | None = None
    max_depth: int = 1
    rel_type: str | None = None
    limit: int = 10
    threshold: float = 0.3


class RawQueryRequest(BaseModel):
    """REM dialect query string, e.g. ``LOOKUP "sarah-chen"``."""

    query: str
    tenant_id: str | None = None
    user_id: UUID | None = None


@router.post("/")
async def execute_query(q: QueryRequest, db: Database = Depends(get_db)):
    """Execute a structured REM query."""
    match q.mode.upper():
        case "LOOKUP":
            return await db.rem_lookup(q.key, tenant_id=q.tenant_id, user_id=q.user_id)
        case "SEARCH":
            if not q.embedding or not q.table:
                raise HTTPException(400, "SEARCH requires embedding and table")
            return await db.rem_search(
                q.embedding, q.table, field=q.field,
                tenant_id=q.tenant_id, min_similarity=q.threshold, limit=q.limit,
            )
        case "FUZZY":
            return await db.rem_fuzzy(
                q.query, tenant_id=q.tenant_id, threshold=q.threshold, limit=q.limit,
            )
        case "TRAVERSE":
            return await db.rem_traverse(
                q.key, tenant_id=q.tenant_id, max_depth=q.max_depth, rel_type=q.rel_type,
            )
        case "SQL":
            if not q.query:
                raise HTTPException(400, "SQL mode requires query")
            return await db.rem_query(q.query, tenant_id=q.tenant_id, user_id=q.user_id)
        case _:
            raise HTTPException(400, f"Unknown mode: {q.mode}")


@router.post("/raw")
async def execute_raw_query(body: RawQueryRequest, db: Database = Depends(get_db)):
    """Execute a REM dialect query string."""
    try:
        results = await db.rem_query(body.query, tenant_id=body.tenant_id, user_id=body.user_id)
        return {"results": results}
    except ValueError as e:
        raise HTTPException(400, str(e))
