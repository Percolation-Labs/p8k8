"""System health reports — HTML email with queue status, usage, CSV attachment.

Data queries live in QueueService and usage.py; this module handles
rendering (HTML/CSV) and sending via EmailService.
"""

from __future__ import annotations

import csv
import io
import logging
from base64 import b64encode
from datetime import datetime, timezone

from p8.ontology.types import User
from p8.services.database import Database
from p8.services.email import EmailService
from p8.services.encryption import EncryptionService
from p8.services.queue import QueueService
from p8.services.usage import REPORT_COLUMNS, get_limits, get_tenant_plans, get_usage_by_tenant
from p8.settings import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Email lookup (decrypt once, reuse everywhere)
# ---------------------------------------------------------------------------

async def build_email_lookup(
    db: Database, encryption: EncryptionService,
) -> dict[str, str]:
    """Build tenant_id -> decrypted email mapping for all active tenants."""
    rows = await db.fetch(
        "SELECT DISTINCT ON (u.tenant_id) u.id, u.tenant_id, u.email "
        "  FROM users u "
        " WHERE u.tenant_id IS NOT NULL AND u.email IS NOT NULL AND u.deleted_at IS NULL "
        " ORDER BY u.tenant_id, u.created_at ASC"
    )
    for r in rows:
        if r["tenant_id"]:
            await encryption.get_dek(r["tenant_id"])

    lookup: dict[str, str] = {}
    for r in rows:
        tid = r["tenant_id"]
        if not tid or not r["email"]:
            continue
        try:
            data = {"id": r["id"], "email": r["email"]}
            decrypted = encryption.decrypt_fields(User, data, tid)
            email = decrypted["email"]
            # If decryption failed silently (sealed mode), email is still ciphertext
            if "@" in email and len(email) < 100:
                lookup[tid] = email
            else:
                lookup[tid] = str(tid)[:8]
        except Exception:
            lookup[tid] = str(tid)[:8]
    return lookup


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

_CSS = """
<style>
  body { font-family: -apple-system, Arial, sans-serif; color: #1a1a1a; margin: 0; padding: 20px; background: #f5f5f5; }
  .container { max-width: 900px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 24px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 24px 0 8px; color: #333; border-bottom: 1px solid #e0e0e0; padding-bottom: 4px; }
  .subtitle { color: #666; font-size: 13px; margin: 0 0 20px; }
  .stats { display: flex; gap: 16px; margin: 16px 0; }
  .stat { background: #f8f8f8; border-radius: 6px; padding: 12px 16px; flex: 1; text-align: center; }
  .stat .num { font-size: 28px; font-weight: 700; }
  .stat .label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat.pending .num { color: #e67e22; }
  .stat.failed .num { color: #e74c3c; }
  .stat.processing .num { color: #3498db; }
  .stat.completed .num { color: #27ae60; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0 16px; }
  th { background: #f0f0f0; text-align: left; padding: 8px; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.3px; }
  td { padding: 8px; border-bottom: 1px solid #eee; }
  tr:hover td { background: #fafafa; }
  .right { text-align: right; }
  .error-cell { color: #c0392b; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { color: #999; font-style: italic; padding: 16px; text-align: center; }
  .footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid #eee; color: #999; font-size: 11px; }
  .over { color: #e74c3c; font-weight: 700; }
  .ok { color: #27ae60; }
  .dim { color: #999; }
</style>
"""


def _fmt_dt(dt: object) -> str:
    if dt is None:
        return "-"
    if hasattr(dt, "strftime"):
        return str(dt.strftime("%Y-%m-%d %H:%M UTC"))
    return str(dt)


def _fmt_short(dt: object) -> str:
    if dt is None:
        return "-"
    if hasattr(dt, "strftime"):
        return str(dt.strftime("%b %d %H:%M"))
    return str(dt)


def _fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return str(n)


def _tenant_email(emails: dict[str, str], tid: str | None) -> str:
    if not tid:
        return "(system)"
    return emails.get(tid, tid[:8])


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_stats_bar(stats: dict[str, int]) -> str:
    html = ""
    for status, css in [("pending", "pending"), ("processing", "processing"),
                        ("failed", "failed"), ("completed", "completed")]:
        count = stats.get(status, 0)
        html += (f'<div class="stat {css}">'
                 f'<div class="num">{count}</div>'
                 f'<div class="label">{status}</div></div>')
    return html


def _render_schedule(
    rows: list[dict],
    emails: dict[str, str],
    task_schedules: dict[str, str],
) -> str:
    if not rows:
        return '<p class="empty">No recurring task history</p>'
    html = '<table><tr><th>User</th><th>Task</th><th>Last Run</th><th>Next Run</th></tr>'
    for r in rows:
        email = _tenant_email(emails, r.get("tenant_id"))
        last = _fmt_short(r.get("last_completed"))
        if r.get("next_pending"):
            nxt = _fmt_short(r["next_pending"])
            nxt_cls = ""
        else:
            # Show cron schedule when no pending task exists
            sched = task_schedules.get(r["task_type"])
            nxt = f"<code>{sched}</code>" if sched else "-"
            nxt_cls = ""
        html += (f"<tr><td>{email}</td><td>{r['task_type']}</td>"
                 f"<td>{last}</td><td{nxt_cls}>{nxt}</td></tr>")
    html += "</table>"
    return html


def _render_cron_jobs(cron_data: dict, emails: dict[str, str]) -> str:
    system = cron_data.get("system", [])
    user_jobs = cron_data.get("user_jobs", [])
    if not system and not user_jobs:
        return '<p class="empty">No active cron jobs</p>'
    html = '<table><tr><th>Job</th><th>Schedule</th><th>Description</th></tr>'
    for r in system:
        html += (f"<tr><td>{r['name']}</td>"
                 f"<td><code>{r['schedule']}</code></td>"
                 f"<td>{r['description']}</td></tr>")
    if user_jobs:
        total = sum(j["count"] for j in user_jobs)
        users = len(user_jobs)
        html += (f"<tr><td>reminders</td>"
                 f"<td>-</td>"
                 f"<td>{total} scheduled across {users} user(s)</td></tr>")
    html += "</table>"
    return html


def _render_queue_table(rows: list[dict], label: str, emails: dict[str, str]) -> str:
    if not rows:
        return f'<p class="empty">No {label.lower()} tasks</p>'
    is_failed = label == "Failed"
    html = "<table><tr><th>Task Type</th><th>Count</th><th>Users</th><th>Earliest</th><th>Latest</th>"
    if is_failed:
        html += "<th>Last Error</th>"
    html += "</tr>"
    for r in rows:
        tenant_ids = r.get("tenant_ids") or []
        users = ", ".join(_tenant_email(emails, tid) for tid in tenant_ids) or "(system)"
        html += (f"<tr><td>{r['task_type']}</td>"
                 f'<td class="right"><strong>{r["cnt"]}</strong></td>'
                 f"<td>{users}</td>"
                 f"<td>{_fmt_dt(r['earliest'])}</td>"
                 f"<td>{_fmt_dt(r['latest'])}</td>")
        if is_failed:
            err = (r.get("last_error") or "")[:120]
            html += f'<td class="error-cell" title="{err}">{err or "-"}</td>'
        html += "</tr>"
    html += "</table>"
    return html


def _render_usage_pivot(
    tenant_usage: dict[str, dict[str, int]],
    tenant_plans: dict[str, str],
    emails: dict[str, str],
) -> str:
    if not tenant_usage:
        return '<p class="empty">No usage data this period</p>'

    html = '<table><tr><th>User</th><th>Plan</th>'
    for _, label, period in REPORT_COLUMNS:
        html += f"<th>{label}<br><small>/{period}</small></th>"
    html += "</tr>"

    for tid in sorted(tenant_usage, key=lambda t: _tenant_email(emails, t)):
        email = _tenant_email(emails, tid)
        plan_id = tenant_plans.get(tid, "free")
        limits = get_limits(plan_id)
        usage = tenant_usage[tid]

        html += f"<tr><td>{email}</td><td>{plan_id}</td>"
        for res_type, _, period in REPORT_COLUMNS:
            used = usage.get(res_type, 0)
            limit = getattr(limits, res_type, 0)
            is_bytes = res_type == "worker_bytes_processed"
            fmt = _fmt_bytes if is_bytes else _fmt_num
            if limit:
                css = "over" if used > limit else "ok"
                html += f'<td class="right"><span class="{css}">{fmt(used)}</span> / {fmt(limit)}</td>'
            elif used:
                html += f'<td class="right">{fmt(used)}</td>'
            else:
                html += '<td class="right dim">-</td>'
        html += "</tr>"

    html += "</table>"
    return html


# ---------------------------------------------------------------------------
# Full HTML assembly
# ---------------------------------------------------------------------------

def render_health_html(
    stats: dict[str, int],
    schedule_rows: list[dict],
    cron_data: dict,
    queued_rows: list[dict],
    failed_rows: list[dict],
    tenant_usage: dict[str, dict[str, int]],
    tenant_plans: dict[str, str],
    total_tasks: int,
    emails: dict[str, str],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    task_schedules = cron_data.get("task_schedules", {})

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">{_CSS}</head>
<body>
<div class="container">
  <h1>p8 System Health Report</h1>
  <p class="subtitle">{now} &middot; {total_tasks} total tasks in queue</p>

  <div class="stats">{_render_stats_bar(stats)}</div>

  <h2>Scheduled Jobs (pg_cron)</h2>
  {_render_cron_jobs(cron_data, emails)}

  <h2>Task Schedule</h2>
  {_render_schedule(schedule_rows, emails, task_schedules)}

  <h2>Queued Tasks (Pending + Processing)</h2>
  {_render_queue_table(queued_rows, "Queued", emails)}

  <h2>Failed Tasks</h2>
  {_render_queue_table(failed_rows, "Failed", emails)}

  <h2>Usage This Period</h2>
  {_render_usage_pivot(tenant_usage, tenant_plans, emails)}

  <div class="footer">
    Attached: task_queue_export.csv with full task details.<br>
    Generated by p8 &middot; Percolation Labs
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "id", "task_type", "tier", "user_email", "user_id", "status", "priority",
    "scheduled_at", "claimed_at", "claimed_by", "started_at", "completed_at",
    "error", "retry_count", "max_retries", "created_at",
]


def build_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: str(r.get(k, "")) for k in _CSV_COLUMNS})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def send_health_report(
    db: Database,
    settings: Settings,
    encryption: EncryptionService,
    *,
    to: str | None = None,
    slack_service=None,
) -> dict:
    """Build and send the system health HTML email with CSV attachment.

    If a SlackService is provided (or settings.slack_bot_token is set),
    also posts a summary + CSV to the Slack alerts channel.
    """
    recipient = to or settings.email_from
    queue = QueueService(db)

    # 1. Tenant emails (shared lookup)
    emails = await build_email_lookup(db, encryption)

    # 2. Queue data (from QueueService)
    stats = await queue.status_counts()
    pending_rows = await queue.summary_by_type("pending")
    processing_rows = await queue.summary_by_type("processing")
    failed_rows = await queue.summary_by_type("failed")
    schedule_rows = await queue.task_schedule()
    cron_data = await queue.cron_jobs()
    all_tasks = await queue.all_tasks()

    for task in all_tasks:
        task["user_email"] = _tenant_email(emails, task.get("tenant_id"))

    # 3. Usage data (from usage.py)
    tenant_usage = await get_usage_by_tenant(db)
    tenant_plans = await get_tenant_plans(db)

    # 4. Render
    queued_rows = pending_rows + processing_rows
    html = render_health_html(
        stats, schedule_rows, cron_data, queued_rows, failed_rows,
        tenant_usage, tenant_plans, len(all_tasks), emails,
    )
    csv_data = build_csv(all_tasks)
    csv_b64 = b64encode(csv_data.encode("utf-8")).decode("ascii")

    plain = (
        f"p8 System Health Report\n"
        f"Pending: {stats.get('pending', 0)} | "
        f"Processing: {stats.get('processing', 0)} | "
        f"Failed: {stats.get('failed', 0)} | "
        f"Completed: {stats.get('completed', 0)}\n"
        f"Total tasks: {len(all_tasks)}\n"
        f"See attached CSV for full details."
    )

    # 5. Slack — post summary + CSV to alerts channel
    _send_to_slack(slack_service, settings, stats, len(all_tasks), csv_data)

    # 6. Email
    email_svc = EmailService(settings)
    if settings.email_provider == "microsoft_graph":
        return await _send_graph_with_attachment(
            email_svc, recipient, html, plain, csv_b64, settings,
        )
    return await email_svc.send(
        to=recipient, subject="p8 System Health Report", body=plain, html=html,
    )


def _send_to_slack(slack_service, settings, stats: dict, total: int, csv_data: str) -> None:
    """Best-effort post of report summary + CSV file to Slack alerts channel."""
    if slack_service is None:
        if not settings.slack_bot_token:
            return
        from p8.services.slack import SlackService
        slack_service = SlackService(None, settings)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = (
        f"*p8 Daily Health Report* ({now})\n"
        f"> Pending: *{stats.get('pending', 0)}* | "
        f"Processing: *{stats.get('processing', 0)}* | "
        f"Failed: *{stats.get('failed', 0)}* | "
        f"Completed: *{stats.get('completed', 0)}*\n"
        f"> Total tasks: {total}"
    )
    slack_service.post_alert(summary)

    if csv_data:
        slack_service.upload_file(
            csv_data,
            filename=f"task_queue_{now.replace(' ', '_').replace(':', '')}.csv",
            initial_comment="Task queue export attached.",
        )


async def _send_graph_with_attachment(
    email_svc: EmailService,
    to: str,
    html: str,
    plain: str,
    csv_b64: str,
    settings: Settings,
) -> dict:
    """Send via Microsoft Graph with CSV file attachment."""
    import httpx

    token = await email_svc._get_graph_token()
    sender = settings.email_from
    url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    payload = {
        "message": {
            "subject": "p8 System Health Report",
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to}}],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "task_queue_export.csv",
                "contentType": "text/csv",
                "contentBytes": csv_b64,
            }],
        },
        "saveToSentItems": True,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 202:
            logger.info("Health report sent to %s", to)
            return {"status": "sent", "to": to}
        logger.error("Graph send failed (%d): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return {"status": "error"}
