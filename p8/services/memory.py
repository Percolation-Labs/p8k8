"""Message loading, compaction, and moment injection."""

from __future__ import annotations

from uuid import UUID

from p8.ontology.types import Message, Moment
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository


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
                "content": f"[Session context]\n{md.get('summary', '')}",
                "token_count": 0,
            })

        # 4. Compact old assistant messages outside the recent window
        #    Breadcrumbs point to the covering moment (resolvable via kv_store)
        #    rather than per-message keys (messages don't sync to kv_store).
        if len(messages) > always_last + 2:
            latest_moment_name = moment_rows[0]["name"] if moment_rows else None
            for i in range(len(messages) - always_last):
                msg = messages[i]
                if msg.get("message_type") == "assistant" and msg.get("content"):
                    if latest_moment_name:
                        messages[i] = {**msg, "content": f"[REM LOOKUP {latest_moment_name}]", "token_count": 10}
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
        content: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        token_count: int | None = None,
        tool_calls: dict | None = None,
    ) -> Message:
        if token_count is None:
            token_count = len(content) // 4 if content else 0

        msg = Message(
            session_id=session_id,
            message_type=message_type,
            content=content,
            token_count=token_count,
            tool_calls=tool_calls,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        [result] = await self.message_repo.upsert(msg)

        await self.db.execute(
            "UPDATE sessions SET total_tokens = total_tokens + $1 WHERE id = $2",
            token_count,
            session_id,
        )
        return result

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
