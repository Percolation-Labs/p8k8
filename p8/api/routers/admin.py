"""Admin endpoints — health, maintenance, diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from p8.api.deps import get_db
from p8.services.database import Database

router = APIRouter()


@router.get("/health")
async def health(db: Database = Depends(get_db)):
    tables = await db.fetchval(
        "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'"
    )
    queue = await db.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='pending') as pending,"
        " COUNT(*) FILTER (WHERE status='failed') as failed"
        " FROM embedding_queue"
    )
    kv = await db.fetchval("SELECT COUNT(*) FROM kv_store")
    return {
        "status": "ok",
        "tables": tables,
        "kv_entries": kv,
        "embedding_queue": {"pending": queue["pending"], "failed": queue["failed"]},
    }


@router.post("/rebuild-kv")
async def rebuild_kv(db: Database = Depends(get_db)):
    await db.execute("SELECT rebuild_kv_store()")
    count = await db.fetchval("SELECT COUNT(*) FROM kv_store")
    return {"rebuilt": True, "entries": count}


@router.get("/queue")
async def embedding_queue_status(db: Database = Depends(get_db)):
    rows = await db.fetch(
        "SELECT table_name, status, COUNT(*) as count"
        " FROM embedding_queue GROUP BY table_name, status"
    )
    return [dict(r) for r in rows]


@router.get("/queue/stats")
async def task_queue_stats(db: Database = Depends(get_db)):
    """Task queue stats — counts by tier and status."""
    rows = await db.fetch(
        "SELECT tier, status, COUNT(*) AS count"
        " FROM task_queue"
        " GROUP BY tier, status"
        " ORDER BY tier, status"
    )
    return {f"{r['tier']}/{r['status']}": r["count"] for r in rows}
