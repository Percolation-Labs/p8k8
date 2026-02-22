"""get_moments tool — query moments with filtering and pagination."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from p8.api.tools import get_db, get_encryption, get_user_id
from p8.ontology.types import Moment
from p8.services.repository import Repository


async def get_moments(
    moment_type: str | None = None,
    category: str | None = None,
    topic_tags: list[str] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Query moments with filtering, date ranges, and pagination.

    Args:
        moment_type: Filter by type (session_chunk, dream, meeting, etc.).
        category: Filter by category string.
        topic_tags: Filter by topic tags (all must match).
        after_date: ISO datetime — return moments created on or after this date.
        before_date: ISO datetime — return moments created on or before this date.
        limit: Max results (1–100, default 20).
        offset: Pagination offset.

    Returns:
        Dict with status, results, count, limit, offset, has_more.
    """
    db = get_db()
    encryption = get_encryption()
    repo = Repository(Moment, db, encryption)

    user_id = get_user_id()
    limit = max(1, min(limit, 100))

    conditions = ["deleted_at IS NULL"]
    params: list = []
    idx = 1

    if user_id:
        conditions.append(f"user_id = ${idx}")
        params.append(user_id)
        idx += 1
    if moment_type:
        conditions.append(f"moment_type = ${idx}")
        params.append(moment_type)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if topic_tags:
        conditions.append(f"topic_tags @> ${idx}")
        params.append(topic_tags)
        idx += 1
    if after_date:
        conditions.append(f"created_at >= ${idx}")
        params.append(datetime.fromisoformat(after_date))
        idx += 1
    if before_date:
        conditions.append(f"created_at <= ${idx}")
        params.append(datetime.fromisoformat(before_date))
        idx += 1

    where = " AND ".join(conditions)
    # Fetch limit+1 to detect has_more
    fetch_limit = limit + 1
    params.extend([fetch_limit, offset])

    try:
        rows = await db.fetch(
            f"SELECT * FROM moments WHERE {where}"
            f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}

    has_more = len(rows) > limit
    rows = rows[:limit]

    await repo._ensure_deks(rows)
    results = []
    for row in rows:
        entity = repo._decrypt_row(row)
        results.append({
            "id": str(entity.id),
            "name": entity.name,
            "moment_type": entity.moment_type,
            "summary": entity.summary,
            "category": entity.category,
            "topic_tags": entity.topic_tags,
            "emotion_tags": entity.emotion_tags,
            "source_session_id": str(entity.source_session_id) if entity.source_session_id else None,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        })

    return {
        "status": "success",
        "results": results,
        "count": len(results),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
