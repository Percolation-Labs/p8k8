"""search tool — execute REM queries against the knowledge base."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from p8.api.tools import get_db, get_user_id


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

    Tables: resources, moments, ontologies, files, sessions, users

    Examples:
    - LOOKUP my-project-plan
    - SEARCH "machine learning pipelines" FROM moments LIMIT 5
    - SEARCH "API gateway architecture" FROM resources LIMIT 3
    - FUZZY project alpha
    - TRAVERSE my-project-plan DEPTH 2

    IMPORTANT: Always use one of the query modes above.
    Do NOT send raw questions or SQL — use SEARCH with keywords instead.

    Args:
        query: REM dialect query string (must start with LOOKUP, SEARCH, FUZZY, or TRAVERSE)
        limit: Maximum results (default 20)
        user_id: Optional user scope

    Returns:
        Query results with entities and metadata
    """
    # Validate query starts with a known REM command
    q = query.strip()
    _VALID_PREFIXES = ("LOOKUP", "SEARCH", "FUZZY", "TRAVERSE", "SQL")
    if not any(q.upper().startswith(p) for p in _VALID_PREFIXES):
        return {
            "status": "error",
            "error": (
                "Invalid query — must start with LOOKUP, SEARCH, FUZZY, or TRAVERSE. "
                'Example: SEARCH "your keywords" FROM moments LIMIT 5'
            ),
            "query": query,
        }

    if user_id is None:
        user_id = get_user_id()

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
