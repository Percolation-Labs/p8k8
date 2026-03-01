"""Admin endpoints — health, maintenance, diagnostics."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from p8.api.deps import get_db, get_encryption
from p8.services.database import Database
from p8.services.encryption import EncryptionService

router = APIRouter()


@router.get("/health")
async def health(db: Database = Depends(get_db)):
    tables = await db.fetchval(
        "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'"
    )
    queue = await db.fetchrow(
        "SELECT COUNT(*) FILTER (WHERE status='pending') as pending,"
        " COUNT(*) FILTER (WHERE status='failed') as failed"
        " FROM embedding_queue"
    )
    kv = await db.fetchval("SELECT COUNT(*) FROM kv_store")

    # pg_cron health: check critical QMS jobs exist and are active
    cron_status = "ok"
    cron_issues: list[str] = []
    reminder_failures = 0
    internal_url = None
    stale_jobs = 0

    has_cron = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = 'cron')"
    )
    has_pgnet = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_net')"
    )
    if has_cron:
        required_jobs = ["qms-recover-stale", "qms-dreaming-enqueue", "qms-news-enqueue"]
        if has_pgnet:
            required_jobs.append("embed-process")

        for job_name in required_jobs:
            row = await db.fetchrow(
                "SELECT j.active, d.status, d.return_message "
                "FROM cron.job j LEFT JOIN LATERAL ("
                "  SELECT status, return_message FROM cron.job_run_details "
                "  WHERE jobid = j.jobid ORDER BY start_time DESC LIMIT 1"
                ") d ON true WHERE j.jobname = $1",
                job_name,
            )
            if not row:
                cron_issues.append(f"{job_name}: MISSING")
                cron_status = "critical"
            elif not row["active"]:
                cron_issues.append(f"{job_name}: INACTIVE")
                cron_status = "critical"
            elif row["status"] and row["status"] != "succeeded":
                cron_issues.append(f"{job_name}: {row['status']} — {(row['return_message'] or '')[:60]}")
                if cron_status != "critical":
                    cron_status = "degraded"

        # pg_net health: check for recent failures in reminder jobs
        reminder_failures = await db.fetchval(
            "SELECT COUNT(*) FROM cron.job j "
            "JOIN cron.job_run_details d ON d.jobid = j.jobid "
            "WHERE j.jobname LIKE 'reminder-%' AND d.status = 'failed' "
            "AND d.start_time > CURRENT_TIMESTAMP - INTERVAL '24 hours'"
        )

        # Check for reminder jobs with hardcoded URLs (legacy)
        stale_jobs = await db.fetchval(
            "SELECT COUNT(*) FROM cron.job "
            "WHERE jobname LIKE 'reminder-%' "
            "AND command NOT LIKE '%current_setting%internal_api_url%'"
        )

    # Check that p8.internal_api_url GUC is set (works without pg_cron)
    internal_url = await db.fetchval(
        "SELECT current_setting('p8.internal_api_url', true)"
    )

    overall = "ok"
    if cron_status == "critical":
        overall = "critical"
    elif not internal_url and has_cron and has_pgnet:
        overall = "critical"
    elif cron_status == "degraded" or reminder_failures > 0 or stale_jobs > 0:
        overall = "degraded"

    return {
        "status": overall,
        "tables": tables,
        "kv_entries": kv,
        "embedding_queue": {"pending": queue["pending"], "failed": queue["failed"]},
        "pg_cron": {
            "status": cron_status,
            "issues": cron_issues or None,
        },
        "pg_net": {
            "internal_api_url": internal_url or "NOT SET",
            "reminder_failures_24h": reminder_failures,
            "stale_hardcoded_jobs": stale_jobs,
        },
    }


@router.post("/rebuild-kv")
async def rebuild_kv(db: Database = Depends(get_db)):
    await db.execute("SELECT rebuild_kv_store()")
    count = await db.fetchval("SELECT COUNT(*) FROM kv_store")
    return {"rebuilt": True, "entries": count}


@router.get("/queue")
async def embedding_queue_status(db: Database = Depends(get_db)):
    rows = await db.fetch(
        "SELECT table_name, status, COUNT(*) as count"
        " FROM embedding_queue GROUP BY table_name, status"
    )
    return [dict(r) for r in rows]


@router.get("/queue/stats")
async def task_queue_stats(db: Database = Depends(get_db)):
    """Task queue stats — counts by tier and status."""
    rows = await db.fetch(
        "SELECT tier, status, COUNT(*) AS count"
        " FROM task_queue"
        " GROUP BY tier, status"
        " ORDER BY tier, status"
    )
    return {f"{r['tier']}/{r['status']}": r["count"] for r in rows}


@router.post("/report")
async def send_report(
    request: Request,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Send the system health report email (+ Slack if configured)."""
    from p8.services.reports import send_health_report

    settings = request.app.state.settings
    slack_service = getattr(request.app.state, "slack_service", None)
    return await send_health_report(db, settings, encryption, slack_service=slack_service)
