"""System health reports — HTML email with queue status + CSV attachment."""

from __future__ import annotations

import csv
import io
import logging
from base64 import b64encode
from datetime import datetime, timezone

from p8.services.database import Database
from p8.services.email import EmailService
from p8.settings import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_SUMMARY_SQL = """
SELECT tq.task_type,
       COUNT(*) AS cnt,
       COUNT(DISTINCT tq.tenant_id) AS tenant_count,
       STRING_AGG(DISTINCT COALESCE(t.name, '(system)'), ', ' ORDER BY COALESCE(t.name, '(system)')) AS tenants,
       MIN(tq.scheduled_at) AS earliest,
       MAX(tq.scheduled_at) AS latest,
       MAX(tq.error) AS last_error
  FROM task_queue tq
  LEFT JOIN tenants t ON tq.tenant_id = t.id::text
 WHERE tq.status = $1
 GROUP BY tq.task_type
 ORDER BY cnt DESC
"""

_ALL_TASKS_SQL = """
SELECT tq.id, tq.task_type, tq.tier,
       COALESCE(t.name, '(system)') AS tenant_name,
       tq.user_id, tq.status, tq.priority,
       tq.scheduled_at, tq.claimed_at, tq.claimed_by,
       tq.started_at, tq.completed_at,
       tq.error, tq.retry_count, tq.max_retries, tq.created_at
  FROM task_queue tq
  LEFT JOIN tenants t ON tq.tenant_id = t.id::text
 ORDER BY tq.created_at DESC
"""

_STATS_SQL = """
SELECT status, COUNT(*) AS cnt FROM task_queue GROUP BY status ORDER BY status
"""


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_CSS = """
<style>
  body { font-family: -apple-system, Arial, sans-serif; color: #1a1a1a; margin: 0; padding: 20px; background: #f5f5f5; }
  .container { max-width: 800px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 24px; }
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
  .error-cell { color: #c0392b; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tenant { font-family: monospace; font-size: 12px; }
  .empty { color: #999; font-style: italic; padding: 16px; text-align: center; }
  .footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid #eee; color: #999; font-size: 11px; }
</style>
"""


def _fmt_dt(dt: object) -> str:
    """Format a datetime for display, or '-' if None."""
    if dt is None:
        return "-"
    if hasattr(dt, "strftime"):
        return str(dt.strftime("%Y-%m-%d %H:%M UTC"))
    return str(dt)


def _fmt_tenant(tenant_id) -> str:
    if not tenant_id:
        return "<em>none</em>"
    s = str(tenant_id)
    return f"{s[:8]}..." if len(s) > 12 else s


def _render_summary_table(rows: list[dict], label: str) -> str:
    if not rows:
        return f'<p class="empty">No {label.lower()} tasks</p>'

    html = "<table><tr><th>Tenant</th><th>Task Type</th><th>Count</th><th>Earliest</th><th>Latest</th>"
    if label == "Failed":
        html += "<th>Last Error</th>"
    html += "</tr>"

    for r in rows:
        html += "<tr>"
        html += f'<td class="tenant">{_fmt_tenant(r["tenant_id"])}</td>'
        html += f'<td>{r["task_type"]}</td>'
        html += f'<td><strong>{r["cnt"]}</strong></td>'
        html += f'<td>{_fmt_dt(r["earliest"])}</td>'
        html += f'<td>{_fmt_dt(r["latest"])}</td>'
        if label == "Failed":
            err = (r.get("last_error") or "")[:120]
            html += f'<td class="error-cell" title="{err}">{err or "-"}</td>'
        html += "</tr>"

    html += "</table>"
    return html


def _render_html(
    stats: dict[str, int],
    pending_rows: list[dict],
    failed_rows: list[dict],
    total_tasks: int,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    stats_html = ""
    for status, css in [("pending", "pending"), ("processing", "processing"), ("failed", "failed"), ("completed", "completed")]:
        count = stats.get(status, 0)
        stats_html += f'<div class="stat {css}"><div class="num">{count}</div><div class="label">{status}</div></div>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">{_CSS}</head>
<body>
<div class="container">
  <h1>p8 System Health Report</h1>
  <p class="subtitle">{now} &middot; {total_tasks} total tasks in queue</p>

  <div class="stats">{stats_html}</div>

  <h2>Queued Tasks (Pending + Processing)</h2>
  {_render_summary_table(pending_rows, "Queued")}

  <h2>Failed Tasks</h2>
  {_render_summary_table(failed_rows, "Failed")}

  <div class="footer">
    Attached: task_queue_export.csv with full task details.<br>
    Generated by p8 &middot; Percolation Labs
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CSV generation
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "id", "task_type", "tier", "tenant_id", "user_id", "status", "priority",
    "scheduled_at", "claimed_at", "claimed_by", "started_at", "completed_at",
    "error", "retry_count", "max_retries", "created_at",
]


def _build_csv(rows: list[dict]) -> str:
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
    *,
    to: str | None = None,
) -> dict:
    """Build and send the system health HTML email with CSV attachment.

    Args:
        db: Database instance.
        settings: App settings.
        to: Recipient email. Defaults to settings.email_from (send to self).
    """
    recipient = to or settings.email_from

    # Run queries
    pending_rows = [dict(r) for r in await db.fetch(_SUMMARY_SQL, "pending")]
    processing_rows = [dict(r) for r in await db.fetch(_SUMMARY_SQL, "processing")]
    failed_rows = [dict(r) for r in await db.fetch(_SUMMARY_SQL, "failed")]
    stats_rows = [dict(r) for r in await db.fetch(_STATS_SQL)]
    all_tasks = [dict(r) for r in await db.fetch(_ALL_TASKS_SQL)]

    # Merge pending + processing for the "queued" table
    queued_rows = pending_rows + processing_rows

    stats = {r["status"]: r["cnt"] for r in stats_rows}

    # Render
    html = _render_html(stats, queued_rows, failed_rows, len(all_tasks))
    csv_data = _build_csv(all_tasks)
    csv_b64 = b64encode(csv_data.encode("utf-8")).decode("ascii")

    # Plain-text fallback
    plain = (
        f"p8 System Health Report\n"
        f"Pending: {stats.get('pending', 0)} | "
        f"Processing: {stats.get('processing', 0)} | "
        f"Failed: {stats.get('failed', 0)} | "
        f"Completed: {stats.get('completed', 0)}\n"
        f"Total tasks: {len(all_tasks)}\n"
        f"See attached CSV for full details."
    )

    # Send via EmailService
    email_svc = EmailService(settings)

    if settings.email_provider == "microsoft_graph":
        # Graph API supports attachments natively
        return await _send_graph_with_attachment(
            email_svc, recipient, html, plain, csv_b64, settings,
        )
    else:
        # For other providers, send HTML only (no attachment support)
        return await email_svc.send(
            to=recipient,
            subject="p8 System Health Report",
            body=plain,
            html=html,
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
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "task_queue_export.csv",
                    "contentType": "text/csv",
                    "contentBytes": csv_b64,
                }
            ],
        },
        "saveToSentItems": True,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 202:
            logger.info("Health report sent to %s", to)
            return {"status": "sent", "to": to, "tasks_exported": len(plain)}
        logger.error("Graph send failed (%d): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return {"status": "error"}
