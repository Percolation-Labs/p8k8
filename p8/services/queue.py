"""Queue service — enqueue, claim, complete, fail tasks with quota integration.

Wraps the SQL functions from 03_qms.sql with Python-level quota checks and
usage tracking. Follows the same patterns as EmbeddingService.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from uuid import UUID

from p8.services.database import Database

log = logging.getLogger(__name__)


class QueueService:
    """Manages the task_queue — enqueue, claim, complete, fail with quotas."""

    def __init__(self, db: Database):
        self.db = db

    async def enqueue(
        self,
        task_type: str,
        payload: dict,
        *,
        tier: str = "small",
        user_id: UUID | None = None,
        tenant_id: str | None = None,
        priority: int = 0,
        scheduled_at: datetime | None = None,
        max_retries: int = 3,
    ) -> UUID:
        """Enqueue a new task. Returns the task ID."""
        row = await self.db.fetchrow(
            "INSERT INTO task_queue"
            " (task_type, tier, user_id, tenant_id, payload, priority, scheduled_at, max_retries)"
            " VALUES ($1, $2, $3, $4, $5::jsonb, $6, COALESCE($7, CURRENT_TIMESTAMP), $8)"
            " RETURNING id",
            task_type,
            tier,
            user_id,
            tenant_id,
            _json_dumps(payload),
            priority,
            scheduled_at,
            max_retries,
        )
        task_id: UUID = row["id"]
        log.info("Enqueued %s task %s (tier=%s)", task_type, task_id, tier)
        return task_id

    async def enqueue_file(
        self,
        file_id: UUID,
        *,
        user_id: UUID | None = None,
        tenant_id: str | None = None,
    ) -> UUID:
        """Enqueue a file processing task. Delegates to SQL for tier assignment."""
        task_id: UUID = await self.db.fetchval(  # type: ignore[assignment]
            "SELECT enqueue_file_task($1, $2, $3)",
            file_id,
            user_id,
            tenant_id,
        )
        log.info("Enqueued file_processing task %s for file %s", task_id, file_id)
        return task_id

    async def claim(
        self,
        tier: str,
        worker_id: str,
        batch_size: int = 1,
    ) -> list[dict]:
        """Claim a batch of pending tasks for the given tier. Returns task dicts."""
        rows = await self.db.fetch(
            "SELECT * FROM claim_tasks($1, $2, $3)",
            tier,
            worker_id,
            batch_size,
        )
        tasks = [dict(r) for r in rows]
        if tasks:
            log.info(
                "Worker %s claimed %d %s task(s): %s",
                worker_id, len(tasks), tier,
                [str(t["id"])[:8] for t in tasks],
            )
        return tasks

    async def complete(self, task_id: UUID, *, result: dict | None = None) -> None:
        """Mark a task as completed with optional result payload."""
        await self.db.execute(
            "SELECT complete_task($1, $2::jsonb)",
            task_id,
            _json_dumps(result) if result else None,
        )
        log.info("Completed task %s", task_id)

    async def fail(self, task_id: UUID, error: str) -> None:
        """Mark a task as failed. SQL handles retry logic."""
        await self.db.execute("SELECT fail_task($1, $2)", task_id, error)
        log.warning("Failed task %s: %s", task_id, error[:200])

    async def emit_event(
        self,
        task_id: UUID,
        event: str,
        *,
        worker_id: str | None = None,
        error: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Write an entry to the task_events audit log."""
        await self.db.execute(
            "SELECT emit_task_event($1, $2, $3, $4, $5::jsonb)",
            task_id,
            event,
            worker_id,
            error,
            _json_dumps(detail) if detail else None,
        )

    async def check_task_quota(self, task: dict) -> bool:
        """Pre-flight quota check before processing a task."""
        user_id = task.get("user_id")
        if not user_id:
            return True  # system tasks have no quota

        task_type = task["task_type"]
        quota_key: str | None = None

        if task_type == "file_processing":
            quota_key = "storage_bytes"
        elif task_type == "dreaming":
            quota_key = "dreaming_minutes"
        elif task_type == "news":
            quota_key = "news_searches_daily"

        if quota_key:
            from p8.services.usage import check_quota, get_user_plan

            plan_id = await get_user_plan(self.db, user_id)
            status = await check_quota(self.db, user_id, quota_key, plan_id)
            if status.exceeded:
                task_id = task.get("id")
                if task_id:
                    await self.emit_event(
                        task_id,
                        "quota_exceeded",
                        error=f"{quota_key} quota exceeded (used={status.used}, limit={status.limit})",
                        detail={"quota": quota_key, "used": status.used, "limit": status.limit, "plan": plan_id},
                    )
                return False

        return True

    async def track_usage(
        self,
        task_id: UUID,
        result: dict,
    ) -> None:
        """Post-completion: store result with usage data and mark complete."""
        await self.complete(task_id, result=result)

    async def stats(self) -> dict:
        """Queue stats: counts by tier and status."""
        rows = await self.db.fetch(
            "SELECT tier, status, COUNT(*) AS count"
            " FROM task_queue"
            " GROUP BY tier, status"
            " ORDER BY tier, status"
        )
        return {f"{r['tier']}/{r['status']}": r["count"] for r in rows}


def _json_dumps(obj: dict | None) -> str | None:
    """Serialize dict to JSON string for PostgreSQL JSONB parameters."""
    if obj is None:
        return None
    return json.dumps(obj)
