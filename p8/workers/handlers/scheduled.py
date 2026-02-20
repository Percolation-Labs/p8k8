"""Scheduled task handler — KV rebuild, embedding backfill, maintenance.

Dispatches by payload.action to the appropriate maintenance routine.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


class ScheduledHandler:
    """Handle scheduled maintenance tasks dispatched via task_queue."""

    async def handle(self, task: dict, ctx) -> dict:
        payload = task.get("payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        action = payload.get("action", "unknown")

        log.info("Scheduled task: action=%s", action)

        if action == "kv_rebuild":
            return await self._kv_rebuild(ctx)
        if action == "kv_rebuild_incremental":
            return await self._kv_rebuild_incremental(ctx)
        if action == "embedding_backfill":
            return await self._embedding_backfill(payload, ctx)

        log.warning("Unknown scheduled action: %s", action)
        return {"status": "unknown_action", "action": action}

    async def _kv_rebuild(self, ctx) -> dict:
        """Full KV store rebuild."""
        await ctx.db.execute("SELECT rebuild_kv_store()")
        count = await ctx.db.fetchval("SELECT COUNT(*) FROM kv_store")
        log.info("KV store rebuilt: %d entries", count)
        return {"action": "kv_rebuild", "entries": count}

    async def _kv_rebuild_incremental(self, ctx) -> dict:
        """Incremental KV store rebuild."""
        count = await ctx.db.fetchval("SELECT rebuild_kv_store_incremental()")
        log.info("KV store incremental rebuild: %d rows updated", count)
        return {"action": "kv_rebuild_incremental", "rows_updated": count}

    async def _embedding_backfill(self, payload: dict, ctx) -> dict:
        """Queue embedding backfill for a specific table."""
        table = payload.get("table")
        if not table:
            return {"action": "embedding_backfill", "error": "no table specified"}

        from p8.services.embeddings import EmbeddingService, create_provider

        provider = create_provider(ctx.settings)
        service = EmbeddingService(
            ctx.db, provider, ctx.encryption,
            batch_size=ctx.settings.embedding_batch_size,
        )
        queued = await service.backfill(table)
        log.info("Embedding backfill queued %d items for %s", queued, table)
        return {"action": "embedding_backfill", "table": table, "queued": queued}
