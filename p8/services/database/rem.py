"""REM function wrappers — mixin for Database class."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg


class RemMixin:
    """Wraps PostgreSQL REM functions as async Python methods.

    Requires ``self.pool`` (asyncpg.Pool) — provided by PoolMixin.
    """

    pool: asyncpg.Pool | None

    async def rem_lookup(
        self, key: str, *, tenant_id: str | None = None, user_id: UUID | None = None
    ) -> list[dict]:
        assert self.pool is not None
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
        min_similarity: float = 0.3,
        limit: int = 10,
        category: str | None = None,
    ) -> list[dict]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM rem_search($1::vector, $2, $3, $4, $5, $6, $7, $8, $9)",
            str(embedding),
            table,
            field,
            tenant_id,
            provider,
            min_similarity,
            limit,
            user_id,
            category,
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
        assert self.pool is not None
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
        load: bool = False,
    ) -> list[dict]:
        """Recursive graph walk. Default lazy mode returns keys + summaries.
        Set load=True to join source tables for full entity data (like LOOKUP)."""
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM rem_traverse("
            "$1::varchar, $2::varchar, $3::uuid, $4::int, $5::varchar, $6::bool, $7::bool)",
            key,
            tenant_id,
            user_id,
            max_depth,
            rel_type,
            False,  # p_keys_only — always false, we use p_load instead
            load,
        )
        return [dict(r) for r in rows]

    async def rem_session_timeline(
        self, session_id: UUID, *, limit: int = 50
    ) -> list[dict]:
        assert self.pool is not None
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
        include_future: bool = False,
    ) -> list[dict]:
        """Cursor-paginated feed.  *before_date* is an ISO date string
        (e.g. ``'2025-02-18'``).  Pass ``None`` for the first page.
        Future-dated moments are excluded unless *include_future* is True."""
        from datetime import date as _date

        bd = _date.fromisoformat(before_date) if before_date else None
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM rem_moments_feed($1, $2, $3, $4)",
            user_id, limit, bd, include_future,
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
        assert self.pool is not None
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
        assert self.pool is not None
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
        moment_threshold: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        model: str | None = None,
        agent_name: str | None = None,
        encryption_level: str | None = None,
        user_msg_id: UUID | None = None,
        asst_msg_id: UUID | None = None,
    ) -> dict:
        """Persist a user+assistant turn atomically. Returns IDs and optional moment name."""
        import json as _json
        tc_json = _json.dumps(tool_calls) if tool_calls else None
        assert self.pool is not None
        row = await self.pool.fetchrow(
            "SELECT * FROM rem_persist_turn("
            "$1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
            session_id, user_content, assistant_content,
            user_id, tenant_id, tc_json, moment_threshold,
            input_tokens, output_tokens, latency_ms, model, agent_name,
            encryption_level, user_msg_id, asst_msg_id,
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
        assert self.pool is not None
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
        min_similarity: float = 0.3,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Search sessions with optional filters. Returns {results, total, page, page_size}."""
        embedding_str = str(query_embedding) if query_embedding else None
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM search_sessions($1, $2, $3, $4, $5, $6::timestamptz,"
            " $7::vector, $8, $9, $10)",
            query, user_id, agent_name, tags, tenant_id, since,
            embedding_str, min_similarity, page, page_size,
        )
        total = rows[0]["total_results"] if rows else 0
        results = [dict(r) for r in rows]
        return {"results": results, "total": total, "page": page, "page_size": page_size}
