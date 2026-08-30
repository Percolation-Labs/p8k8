"""Percolate ingestion handler — runs the detection pipeline for one source.

pg_cron's enqueue_percolate_ingest_tasks() (sql/05_percolate.sql) creates one
'percolate_ingest' task per enabled source every 30 minutes; this handler runs
the full ingest -> resolve -> score -> threshold -> interpret pipeline
(p8/services/percolate/pipeline.py) for that source.
"""

from __future__ import annotations

import logging

from p8.services.percolate.pipeline import run_ingestion_cycle

log = logging.getLogger(__name__)


class PercolateIngestHandler:
    """Background handler for task_type='percolate_ingest'."""

    async def handle(self, task: dict, ctx) -> dict:
        payload = task.get("payload") or {}
        source_key = payload.get("source_key")
        source_keys = [source_key] if source_key else None

        stats = await run_ingestion_cycle(
            db=ctx.db,
            encryption=ctx.encryption,
            embedding_service=ctx.embedding_service,
            settings=ctx.settings,
            source_keys=source_keys,
        )

        if stats["errors"] and stats["items_ingested"] == 0:
            return {"status": f"ingestion_failed: {'; '.join(stats['errors'])}", **stats}

        return {"status": "ok", **stats}
