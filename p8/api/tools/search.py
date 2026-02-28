"""search tool — execute REM queries against the knowledge base."""

from __future__ import annotations

import logging
import re
from typing import Any

from p8.api.tools import get_db, get_encryption, get_user_id
from p8.ontology.types import TABLE_MAP

logger = logging.getLogger(__name__)

# Matches the first FROM <table> in a SQL query.
_SQL_TABLE_RE = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)


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


def _decrypt_sql_results(results: list[dict], sql: str) -> list[dict]:
    """Decrypt flat SQL result rows in place.

    SQL mode returns plain dicts (not the ``{entity_type, data}`` wrapper
    that REM functions produce).  We parse the table from the query, look
    up the model, and decrypt any encrypted fields if ``encryption_level``
    and ``tenant_id`` are present in the row.
    """
    m = _SQL_TABLE_RE.search(sql)
    if not m:
        return results

    table = m.group(1).lower()
    model_class = TABLE_MAP.get(table)
    if not model_class or not getattr(model_class, "__encrypted_fields__", None):
        return results

    encryption = get_encryption()
    out = []
    for row in results:
        if row.get("encryption_level") != "platform" or not row.get("tenant_id"):
            out.append(row)
            continue
        try:
            out.append(encryption.decrypt_fields(model_class, dict(row), row["tenant_id"]))
        except Exception:
            logger.debug("search: SQL decrypt failed for %s/%s", table, row.get("id"))
            out.append(row)
    return out


def _ensure_decrypt_columns(query: str) -> str:
    """Inject id, tenant_id, encryption_level into a SQL SELECT if missing.

    Decryption needs these columns.  We add them transparently so the agent
    doesn't have to remember to include them in every query.  The extra
    columns are stripped from the final output by ``_strip_added_columns``.
    """
    m = _SQL_TABLE_RE.search(query)
    if not m:
        return query

    table = m.group(1).lower()
    model_class = TABLE_MAP.get(table)
    if not model_class or not getattr(model_class, "__encrypted_fields__", None):
        return query  # not an encrypted table — nothing to add

    # Only modify explicit column SELECTs, not SELECT *
    upper = query.upper()
    sql_body = query[3:].strip() if upper.startswith("SQL") else query  # strip "SQL " prefix
    if re.match(r"SELECT\s+\*", sql_body, re.IGNORECASE):
        return query  # SELECT * already includes everything

    needed = {"id", "tenant_id", "encryption_level"}
    present = {c.strip().lower() for c in re.split(r"[,\s]+", sql_body.split("FROM")[0].replace("SELECT", "", 1))}
    missing = needed - present
    if not missing:
        return query

    # Insert missing columns right after SELECT
    additions = ", ".join(missing)
    return re.sub(
        r"(SQL\s+SELECT\s+)",
        rf"\g<1>{additions}, ",
        query,
        count=1,
        flags=re.IGNORECASE,
    )


def _strip_added_columns(results: list[dict], original_query: str, effective_query: str) -> list[dict]:
    """Remove columns that were auto-added by ``_ensure_decrypt_columns``."""
    if original_query == effective_query:
        return results  # nothing was added

    # Figure out which columns were added
    orig_body = original_query[3:].strip() if original_query.upper().startswith("SQL") else original_query
    orig_cols = {c.strip().lower() for c in re.split(r"[,\s]+", orig_body.split("FROM")[0].replace("SELECT", "", 1))}
    added = {"id", "tenant_id", "encryption_level"} - orig_cols

    if not added:
        return results

    return [{k: v for k, v in row.items() if k not in added} for row in results]


async def _warm_dek_cache(results: list[dict], *, sql: str | None = None) -> None:
    """Pre-cache DEKs for all tenant_ids in results so decrypt_fields is fast."""
    encryption = get_encryption()
    tenant_ids = set()
    for r in results:
        # REM wrapper format
        data = r.get("data")
        if isinstance(data, dict) and data.get("encryption_level") == "platform":
            tid = data.get("tenant_id")
            if tid:
                tenant_ids.add(tid)
        # Flat SQL format
        if r.get("encryption_level") == "platform":
            tid = r.get("tenant_id")
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
    is_sql = q.upper().startswith("SQL")

    # For SQL queries against encrypted tables, ensure the columns needed
    # for decryption (id, tenant_id, encryption_level) are fetched.
    effective_query = query
    if is_sql:
        effective_query = _ensure_decrypt_columns(q)

    db = get_db()
    try:
        results = await db.rem_query(effective_query, user_id=user_id)

        # Decrypt: SQL results are flat dicts, REM results use {entity_type, data}.
        await _warm_dek_cache(results, sql=q if is_sql else None)
        if is_sql:
            results = _decrypt_sql_results(results, q)
            # Strip helper columns that were auto-added for decryption.
            results = _strip_added_columns(results, query, effective_query)
        else:
            results = _decrypt_results(results)

        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "query": query}
