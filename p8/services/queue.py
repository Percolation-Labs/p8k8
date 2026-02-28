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
        elif task_type == "drive_sync":
            quota_key = "drive_syncs_daily"

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

    async def status_counts(self) -> dict[str, int]:
        """Return {status: count} across all tiers."""
        rows = await self.db.fetch(
            "SELECT status, COUNT(*) AS cnt FROM task_queue GROUP BY status ORDER BY status"
        )
        return {r["status"]: r["cnt"] for r in rows}

    async def summary_by_type(self, status: str) -> list[dict]:
        """Return queue summary grouped by task_type for a given status.

        Each row: task_type, cnt, tenant_ids[], earliest, latest, last_error.
        """
        return [dict(r) for r in await self.db.fetch(
            "SELECT tq.task_type, COUNT(*) AS cnt, "
            "       ARRAY_AGG(DISTINCT tq.tenant_id) "
            "           FILTER (WHERE tq.tenant_id IS NOT NULL) AS tenant_ids, "
            "       MIN(tq.scheduled_at) AS earliest, "
            "       MAX(tq.scheduled_at) AS latest, "
            "       MAX(tq.error) AS last_error "
            "  FROM task_queue tq "
            " WHERE tq.status = $1 "
            " GROUP BY tq.task_type "
            " ORDER BY cnt DESC",
            status,
        )]

    async def all_tasks(self) -> list[dict]:
        """Return all tasks ordered by created_at DESC (for CSV export)."""
        return [dict(r) for r in await self.db.fetch(
            "SELECT id, task_type, tier, tenant_id, user_id, status, priority, "
            "       scheduled_at, claimed_at, claimed_by, started_at, completed_at, "
            "       error, retry_count, max_retries, created_at "
            "  FROM task_queue ORDER BY created_at DESC"
        )]

    async def task_schedule(self) -> list[dict]:
        """Return last completed + next pending per (task_type, tenant_id).

        Cross-joins all active tenants with recurring task types so every
        user appears even if they have no task history yet.
        """
        rows = await self.db.fetch(
            "SELECT t.task_type, u.tenant_id, "
            "       MAX(tq.completed_at) FILTER (WHERE tq.status = 'completed') AS last_completed, "
            "       MIN(tq.scheduled_at) FILTER (WHERE tq.status = 'pending') AS next_pending "
            "  FROM (VALUES ('dreaming'), ('news'), ('drive_sync')) AS t(task_type) "
            " CROSS JOIN (SELECT DISTINCT tenant_id FROM users "
            "             WHERE tenant_id IS NOT NULL AND deleted_at IS NULL) u "
            "  LEFT JOIN task_queue tq "
            "    ON tq.task_type = t.task_type AND tq.tenant_id = u.tenant_id "
            " GROUP BY t.task_type, u.tenant_id "
            " ORDER BY u.tenant_id, t.task_type"
        )
        return [dict(r) for r in rows]

    async def cron_jobs(self) -> dict:
        """Return active pg_cron jobs, classified into system + user summary.

        Returns dict with:
          system: list of {name, schedule, description} for system-wide jobs
          user_jobs: list of {user, count} for per-user jobs (reminders etc.)
          task_schedules: {task_type: schedule} mapping for recurring task types
        """
        try:
            rows = await self.db.fetch(
                "SELECT jobid, jobname, schedule, command, active FROM cron.job "
                "WHERE active = true ORDER BY jobid"
            )
        except Exception:
            return {"system": [], "user_jobs": [], "task_schedules": {}}

        import re
        from collections import defaultdict

        system = []
        user_counts: dict[str, int] = defaultdict(int)
        task_schedules: dict[str, str] = {}

        for r in rows:
            cmd = r["command"] or ""
            name = r["jobname"] or ""

            if "enqueue_news_tasks" in cmd:
                task_schedules["news"] = r["schedule"]
                system.append({"name": name, "schedule": r["schedule"],
                               "description": "Enqueue news digests for all users"})
            elif "enqueue_dreaming_tasks" in cmd:
                task_schedules["dreaming"] = r["schedule"]
                system.append({"name": name, "schedule": r["schedule"],
                               "description": "Enqueue dreaming for all users"})
            elif "recover_stale_tasks" in cmd:
                system.append({"name": name, "schedule": r["schedule"],
                               "description": "Recover stale/stuck tasks"})
            elif "reminder-" in name or "/notifications/send" in cmd:
                # Extract user identifier from the command URL
                m = re.search(r'/users/([0-9a-f-]{8,36})/', cmd)
                user_key = m.group(1)[:8] if m else "unknown"
                user_counts[user_key] += 1
            else:
                system.append({"name": name, "schedule": r["schedule"],
                               "description": cmd[:60]})

        user_jobs = [{"user": u, "count": c} for u, c in sorted(user_counts.items())]
        return {"system": system, "user_jobs": user_jobs, "task_schedules": task_schedules}


def _json_dumps(obj: dict | None) -> str | None:
    """Serialize dict to JSON string for PostgreSQL JSONB parameters."""
    if obj is None:
        return None
    return json.dumps(obj)
