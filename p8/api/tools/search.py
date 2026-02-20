"""search tool — execute REM queries against the knowledge base."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from p8.api.tools import get_db


async def search(
    query: str,
    limit: int = 20,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Execute REM queries to search the knowledge base.

    Query Syntax:
    - LOOKUP <key>: O(1) exact entity lookup by key
    - SEARCH <text> FROM <table>: Semantic vector search
    - FUZZY <text>: Fuzzy text matching across all entities
    - TRAVERSE <key> DEPTH <n>: Graph traversal from entity
    - SQL <query>: Direct SQL (SELECT only)

    Examples:
    - search("LOOKUP sarah-chen")
    - search("SEARCH machine learning FROM ontologies LIMIT 5")
    - search("FUZZY project alpha")

    Args:
        query: REM dialect query string
        limit: Maximum results (default 20)
        user_id: Optional user scope

    Returns:
        Query results with entities and metadata
    """
    db = get_db()
    try:
        results = await db.rem_query(query, user_id=user_id)
        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "query": query}
