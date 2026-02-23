"""Message loading, compaction, and moment injection."""

from __future__ import annotations

from uuid import UUID

from p8.ontology.types import Message, Moment, Session
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository
from p8.utils.tokens import estimate_tokens


def format_moment_context(md: dict) -> str:
    """Format a moment dict as enriched session context for agent injection.

    Includes summary, resource keys, file name, and topic tags when available.
    Skips metadata lines already present in the summary (e.g. upload moments
    embed Resources: in their summary text).
    Used by both MemoryService.load_context() and adapter._load_session_moments().
    """
    summary = md.get("summary", "")
    parts = [f"[Session context]\n{summary}"]
    meta = md.get("metadata") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if meta.get("resource_keys") and "Resources:" not in summary:
        parts.append(f"Resources: {', '.join(meta['resource_keys'][:10])}")
    if meta.get("file_name") and "File:" not in summary:
        parts.append(f"File: {meta['file_name']}")
    tags = md.get("topic_tags") or []
    if tags:
        parts.append(f"Topics: {', '.join(tags)}")
    return "\n".join(parts)


class MemoryService:
    def __init__(self, db: Database, encryption: EncryptionService):
        self.db = db
        self.encryption = encryption
        self.message_repo = Repository(Message, db, encryption)
        self.moment_repo = Repository(Moment, db, encryption)

    async def load_context(
        self,
        session_id: UUID,
        *,
        max_tokens: int | None = 8000,
        max_messages: int | None = None,
        since: str | None = None,
        always_last: int = 5,
        max_moments: int = 3,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """Load messages within token/message budget, with compaction and moment injection."""
        # Ensure DEK cached for decryption
        if tenant_id:
            await self.encryption.get_dek(tenant_id)

        # 1. Database-side token-aware loading
        raw = await self.db.rem_load_messages(
            session_id, max_tokens=max_tokens, max_messages=max_messages, since=since
        )

        # 2. Decrypt content fields
        messages = []
        for row in raw:
            data = dict(row)
            data = self.encryption.decrypt_fields(Message, data, tenant_id)
            messages.append(data)

        # 3. Inject last N moment summaries for temporal grounding (oldest first)
        moment_rows = await self.db.fetch(
            "SELECT * FROM moments"
            " WHERE source_session_id = $1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT $2",
            session_id,
            max_moments,
        )
        for mrow in reversed(moment_rows):
            md = dict(mrow)
            md = self.encryption.decrypt_fields(Moment, md, tenant_id)
            messages.insert(0, {
                "message_type": "system",
                "content": format_moment_context(md),
                "token_count": 0,
            })

        # 4. Compact old assistant messages outside the recent window
        #    Breadcrumbs point to the covering moment (resolvable via kv_store)
        #    rather than per-message keys (messages don't sync to kv_store).
        if len(messages) > always_last + 2:
            latest_moment_name = moment_rows[0]["name"] if moment_rows else None
            moment_hint = (moment_rows[0].get("summary", "")[:120] if moment_rows else "")
            for i in range(len(messages) - always_last):
                msg = messages[i]
                if msg.get("message_type") == "assistant" and msg.get("content"):
                    if latest_moment_name:
                        messages[i] = {
                            **msg,
                            "content": f"[Earlier: {moment_hint}… → REM LOOKUP {latest_moment_name}]",
                            "token_count": 20,
                        }
                    else:
                        messages[i] = {**msg, "content": "[earlier message compacted]", "token_count": 5}

        return messages

    # ------------------------------------------------------------------
    # Moment building — delegates to rem_build_moment() SQL function
    # ------------------------------------------------------------------

    async def build_moment(
        self,
        session_id: UUID,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> Moment | None:
        """Build a session_chunk moment from messages since the last moment.

        Delegates entirely to the rem_build_moment() SQL function which
        atomically finds messages, aggregates stats, generates the name,
        upserts the moment, and updates session metadata.
        """
        row = await self.db.rem_build_moment(
            session_id, tenant_id=tenant_id, user_id=user_id, threshold=0,
        )
        if not row:
            return None
        return self._moment_from_row(row)

    async def maybe_build_moment(
        self,
        session_id: UUID,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        threshold: int = 6000,
    ) -> Moment | None:
        """Build a moment if tokens since last moment exceed threshold.

        Single DB round-trip via rem_build_moment(threshold).
        Returns None if below threshold or no messages.
        """
        row = await self.db.rem_build_moment(
            session_id, tenant_id=tenant_id, user_id=user_id, threshold=threshold,
        )
        if not row:
            return None
        return self._moment_from_row(row)

    async def build_today_summary(self, *, user_id: UUID | None = None) -> dict | None:
        """Virtual 'today' moment — delegates to rem_moments_feed().

        Fetches the most recent daily_summary from the existing SQL function
        and returns it in the expected dict shape, or None if no activity today.
        """
        from datetime import date

        rows = await self.db.rem_moments_feed(user_id=user_id, limit=1)
        for r in rows:
            if r.get("event_type") == "daily_summary" and r.get("event_date") == date.today():
                meta = r.get("metadata", {})
                return {
                    "name": "today",
                    "moment_type": "today_summary",
                    "summary": r.get("summary", ""),
                    "metadata": {
                        "message_count": meta.get("message_count", 0),
                        "total_tokens": meta.get("total_tokens", 0),
                        "moment_count": meta.get("moment_count", 0),
                        "sessions": meta.get("sessions", []),
                    },
                }
        return None

    # ------------------------------------------------------------------
    # Message persistence
    # ------------------------------------------------------------------

    async def persist_message(
        self,
        session_id: UUID,
        message_type: str,
        content: str | None,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        token_count: int | None = None,
        tool_calls: dict | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        encryption_level: str | None = None,
    ) -> Message:
        if token_count is None:
            token_count = estimate_tokens(content or "")

        msg = Message(
            session_id=session_id,
            message_type=message_type,
            content=content,
            token_count=token_count,
            tool_calls=tool_calls,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=agent_name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            encryption_level=encryption_level,
        )
        results = await self.message_repo.upsert(msg)
        result: Message = results[0]

        await self.db.execute(
            "UPDATE sessions SET total_tokens = total_tokens + $1 WHERE id = $2",
            token_count,
            session_id,
        )
        return result

    # ------------------------------------------------------------------
    # Moment + Session creation (companion session pattern)
    # ------------------------------------------------------------------

    async def create_moment_session(
        self,
        *,
        name: str,
        moment_type: str,
        summary: str,
        metadata: dict | None = None,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        tenant_id: str | None = None,
        topic_tags: list[str] | None = None,
        image_uri: str | None = None,
        starts_timestamp=None,
        ends_timestamp=None,
        graph_edges: list[dict] | None = None,
        session_description: str | None = None,
    ) -> tuple[Moment, Session]:
        """Create a moment and ensure it has a companion session with context.

        Every moment gets a 1:1 companion session so users can start a
        conversation about it.  The session stores the moment's name,
        summary, and metadata so the agent has context via ContextInjector
        even before any messages exist.

        If ``session_id`` is provided and the session already exists, its
        metadata is enriched with the new moment info (supports multiple
        uploads to the same session).

        Returns (moment, session) tuple.
        """
        meta = metadata or {}

        # 1. Create the moment
        moment = Moment(
            name=name,
            moment_type=moment_type,
            summary=summary,
            image_uri=image_uri,
            source_session_id=session_id,
            starts_timestamp=starts_timestamp,
            ends_timestamp=ends_timestamp,
            topic_tags=topic_tags or [],
            graph_edges=graph_edges or [],
            metadata=meta,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        [moment] = await self.moment_repo.upsert(moment)

        # 2. Ensure companion session
        session_repo = Repository(Session, self.db, self.encryption)
        session_meta = {
            "moment_id": str(moment.id),
            "moment_name": name,
            "moment_type": moment_type,
            **{k: v for k, v in meta.items() if k != "moment_id"},
        }

        if session_id:
            existing = await session_repo.get(session_id)
            if existing:
                # Merge into existing session metadata
                merged = existing.metadata or {}
                uploads = merged.get("uploads", [])
                uploads.append(session_meta)
                merged["uploads"] = uploads
                # Accumulate resource_keys for easy agent lookup
                all_keys = merged.get("resource_keys", [])
                all_keys.extend(meta.get("resource_keys", []))
                merged["resource_keys"] = all_keys
                merged["latest_moment_id"] = str(moment.id)
                merged["latest_summary"] = summary[:200]
                existing.metadata = merged
                if not existing.description and summary:
                    existing.description = summary[:500]
                await session_repo.upsert(existing)
                # Update moment to point to this session
                if not moment.source_session_id:
                    await self.db.execute(
                        "UPDATE moments SET source_session_id = $1 WHERE id = $2",
                        session_id, moment.id,
                    )
                    moment.source_session_id = session_id
                session = existing
            else:
                session = Session(
                    id=session_id,
                    name=name,
                    description=(session_description or summary)[:500] if summary else None,
                    mode=moment_type,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    metadata={**session_meta, "uploads": [session_meta]},
                )
                await session_repo.upsert(session)
                moment.source_session_id = session_id
                await self.db.execute(
                    "UPDATE moments SET source_session_id = $1 WHERE id = $2",
                    session_id, moment.id,
                )
        else:
            session = Session(
                name=name,
                description=(session_description or summary)[:500] if summary else None,
                mode=moment_type,
                user_id=user_id,
                tenant_id=tenant_id,
                metadata={**session_meta, "uploads": [session_meta]},
            )
            [session] = await session_repo.upsert(session)
            await self.db.execute(
                "UPDATE moments SET source_session_id = $1 WHERE id = $2",
                session.id, moment.id,
            )
            moment.source_session_id = session.id

        return moment, session

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _moment_from_row(row: dict) -> Moment:
        """Convert a rem_build_moment() result row to a Moment instance."""
        return Moment(
            id=row["moment_id"],
            name=row["moment_name"],
            moment_type=row["moment_type"],
            summary=row["summary"],
            source_session_id=row.get("source_session_id"),
            starts_timestamp=row["starts_timestamp"],
            ends_timestamp=row["ends_timestamp"],
            previous_moment_keys=row.get("previous_keys") or [],
            metadata={
                "message_count": row["message_count"],
                "token_count": row["token_count"],
                "chunk_index": row["chunk_index"],
            },
        )
