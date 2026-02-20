"""REM function wrappers — mixin for Database class."""

from __future__ import annotations

from uuid import UUID


class RemMixin:
    """Wraps PostgreSQL REM functions as async Python methods.

    Requires ``self.pool`` (asyncpg.Pool) — provided by PoolMixin.
    """

    async def rem_lookup(
        self, key: str, *, tenant_id: str | None = None, user_id: UUID | None = None
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_lookup($1, $2, $3)", key, tenant_id, user_id
        )
        return [{"entity_type": r["entity_type"], "data": r["data"]} for r in rows]

    async def rem_search(
        self,
        embedding: list[float],
        table: str,
        *,
        field: str = "content",
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        provider: str = "openai",
        min_similarity: float = 0.7,
        limit: int = 10,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_search($1::vector, $2, $3, $4, $5, $6, $7, $8)",
            str(embedding),
            table,
            field,
            tenant_id,
            provider,
            min_similarity,
            limit,
            user_id,
        )
        return [dict(r) for r in rows]

    async def rem_fuzzy(
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        threshold: float = 0.3,
        limit: int = 10,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_fuzzy($1, $2, $3, $4, $5)",
            query, tenant_id, threshold, limit, user_id,
        )
        return [dict(r) for r in rows]

    async def rem_traverse(
        self,
        key: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        max_depth: int = 1,
        rel_type: str | None = None,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_traverse($1, $2, $3, $4, $5)",
            key,
            tenant_id,
            user_id,
            max_depth,
            rel_type,
        )
        return [dict(r) for r in rows]

    async def rem_session_timeline(
        self, session_id: UUID, *, limit: int = 50
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_session_timeline($1, $2)", session_id, limit
        )
        return [dict(r) for r in rows]

    async def rem_moments_feed(
        self,
        *,
        user_id: UUID | None = None,
        limit: int = 20,
        before_date: str | None = None,
    ) -> list[dict]:
        """Cursor-paginated feed.  *before_date* is an ISO date string
        (e.g. ``'2025-02-18'``).  Pass ``None`` for the first page."""
        from datetime import date as _date

        bd = _date.fromisoformat(before_date) if before_date else None
        rows = await self.pool.fetch(
            "SELECT * FROM rem_moments_feed($1, $2, $3)", user_id, limit, bd
        )
        return [dict(r) for r in rows]

    async def rem_load_messages(
        self,
        session_id: UUID,
        *,
        max_tokens: int | None = None,
        max_messages: int | None = None,
        since: str | None = None,
    ) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM rem_load_messages($1, $2, $3, $4::timestamptz)",
            session_id,
            max_tokens,
            max_messages,
            since,
        )
        return [dict(r) for r in rows]

    async def rem_build_moment(
        self,
        session_id: UUID,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        threshold: int = 0,
    ) -> dict | None:
        """Build a session_chunk moment. Returns moment dict or None if below threshold."""
        row = await self.pool.fetchrow(
            "SELECT * FROM rem_build_moment($1, $2, $3, $4)",
            session_id, tenant_id, user_id, threshold,
        )
        return dict(row) if row else None

    async def rem_persist_turn(
        self,
        session_id: UUID,
        user_content: str,
        assistant_content: str,
        *,
        user_id: UUID | None = None,
        tenant_id: str | None = None,
        tool_calls: dict | None = None,
        pai_messages: str | None = None,
        moment_threshold: int = 0,
    ) -> dict:
        """Persist a user+assistant turn atomically. Returns IDs and optional moment name."""
        import json as _json
        tc_json = _json.dumps(tool_calls) if tool_calls else None
        pai_json = pai_messages  # already a JSON string from caller
        row = await self.pool.fetchrow(
            "SELECT * FROM rem_persist_turn($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8)",
            session_id, user_content, assistant_content,
            user_id, tenant_id, tc_json, pai_json, moment_threshold,
        )
        return dict(row) if row else {}

    async def clone_session(
        self,
        source_session_id: UUID,
        *,
        max_messages: int | None = None,
        new_user_id: UUID | None = None,
        new_agent_name: str | None = None,
    ) -> dict:
        """Clone a session and its messages. Returns {new_session_id, messages_copied}."""
        row = await self.pool.fetchrow(
            "SELECT * FROM clone_session($1, $2, $3, $4)",
            source_session_id,
            max_messages,
            new_user_id,
            new_agent_name,
        )
        return {"new_session_id": row["new_session_id"], "messages_copied": row["messages_copied"]}

    async def search_sessions(
        self,
        *,
        query: str | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        tags: list[str] | None = None,
        tenant_id: str | None = None,
        since: str | None = None,
        query_embedding: list[float] | None = None,
        min_similarity: float = 0.7,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Search sessions with optional filters. Returns {results, total, page, page_size}."""
        embedding_str = str(query_embedding) if query_embedding else None
        rows = await self.pool.fetch(
            "SELECT * FROM search_sessions($1, $2, $3, $4, $5, $6::timestamptz,"
            " $7::vector, $8, $9, $10)",
            query, user_id, agent_name, tags, tenant_id, since,
            embedding_str, min_similarity, page, page_size,
        )
        total = rows[0]["total_results"] if rows else 0
        results = [dict(r) for r in rows]
        return {"results": results, "total": total, "page": page, "page_size": page_size}
