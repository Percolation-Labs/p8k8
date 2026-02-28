"""search tool — execute REM queries against the knowledge base."""

from __future__ import annotations

import logging
from typing import Any

from p8.api.tools import get_db, get_encryption, get_user_id
from p8.ontology.types import TABLE_MAP

logger = logging.getLogger(__name__)


def _decrypt_results(results: list[dict]) -> list[dict]:
    """Decrypt platform-encrypted fields in REM query results.

    REM functions return raw row_to_json(t.*) which may contain encrypted
    content fields.  This post-processes results so agents see plaintext.
    """
    encryption = get_encryption()

    out = []
    for result in results:
        data = result.get("data")
        if not isinstance(data, dict):
            out.append(result)
            continue

        entity_type = result.get("entity_type") or data.get("type")
        level = data.get("encryption_level")

        if level != "platform" or not entity_type:
            out.append(result)
            continue

        model_class = TABLE_MAP.get(entity_type)
        if not model_class or not getattr(model_class, "__encrypted_fields__", None):
            out.append(result)
            continue

        tenant_id = data.get("tenant_id")
        if not tenant_id:
            out.append(result)
            continue

        try:
            decrypted = encryption.decrypt_fields(model_class, data, tenant_id)
            out.append({**result, "data": decrypted})
        except Exception:
            logger.debug("search: decrypt failed for %s/%s", entity_type, data.get("id"))
            out.append(result)

    return out


async def _warm_dek_cache(results: list[dict]) -> None:
    """Pre-cache DEKs for all tenant_ids in results so decrypt_fields is fast."""
    encryption = get_encryption()
    tenant_ids = set()
    for r in results:
        data = r.get("data")
        if isinstance(data, dict) and data.get("encryption_level") == "platform":
            tid = data.get("tenant_id")
            if tid:
                tenant_ids.add(tid)

    for tid in tenant_ids:
        await encryption.get_dek(tid)


async def search(
    query: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Execute REM queries to search the knowledge base.

    Query Syntax:
    - LOOKUP <key>: O(1) exact entity lookup by key
    - SEARCH <text> FROM <table>: Semantic vector search
    - FUZZY <text>: Fuzzy text matching across all entities
    - TRAVERSE <key> DEPTH <n>: Graph traversal from entity
    - SQL <query>: Direct SQL query against core tables

    Core Tables:
    - **moments** — User activity: session summaries, uploads, dreams, reminders, web searches.
      Each has moment_type, summary, topic_tags, category, metadata (JSONB), created_at.
    - **resources** — Uploaded files, documents, bookmarked URLs, RSS articles.
      Each has name, summary, tags, metadata, created_at.
    - **ontologies** — Knowledge base: concepts, documentation, system info.

    All core tables have `created_at`, `updated_at`, and `deleted_at` columns.
    Filter active records with `WHERE deleted_at IS NULL`.

    Examples:
    - LOOKUP my-project-plan
    - SEARCH "machine learning pipelines" FROM moments LIMIT 5
    - SEARCH "API gateway architecture" FROM resources LIMIT 3
    - FUZZY project alpha
    - TRAVERSE my-project-plan DEPTH 2
    - SQL SELECT name, moment_type, summary, created_at FROM moments WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 10
    - SQL SELECT name, moment_type, summary, metadata FROM moments WHERE moment_type = 'content_upload' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 5
    - SQL SELECT name, summary, tags FROM resources WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 10

    IMPORTANT: Always use one of the query modes above.
    Do NOT send raw questions — use SEARCH with keywords or SQL for date-based queries.

    Args:
        query: REM dialect query string (must start with LOOKUP, SEARCH, FUZZY, TRAVERSE, or SQL)
        limit: Maximum results (default 20)

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
                "Invalid query — must start with LOOKUP, SEARCH, FUZZY, TRAVERSE, or SQL. "
                'Example: SEARCH "your keywords" FROM moments LIMIT 5'
            ),
            "query": query,
        }

    user_id = get_user_id()

    db = get_db()
    try:
        results = await db.rem_query(query, user_id=user_id)
        # Decrypt platform-encrypted content before returning to agent
        await _warm_dek_cache(results)
        results = _decrypt_results(results)
        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "query": query}
