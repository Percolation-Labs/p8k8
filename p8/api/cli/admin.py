"""p8 admin — queue health, schedule diagnostics, user quota reports.

Defaults to REMOTE (Hetzner p8-w-1 via port-forward on localhost:5491).
Use --local to target the local docker-compose Postgres on localhost:5489.

All output uses Rich tables.
"""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

import p8.services.bootstrap as _svc

admin_app = typer.Typer(no_args_is_help=True)
_con = Console(width=130)

# Port-forward conventions (remote Hetzner → localhost)
REMOTE_PG_PORT = 5491          # kubectl port-forward svc/p8-postgres-rw 5491:5432
REMOTE_VAULT_PORT = 8201       # kubectl port-forward svc/openbao 8201:8200
LOCAL_PG_PORT = 5489           # docker-compose default

# Remote DB conventions (CNPG cluster uses different user/db than local dev)
REMOTE_DB_USER = "p8user"
REMOTE_DB_NAME = "p8db"

# Known task types and their pg_cron source
_TASK_TYPES = {
    "dreaming": "qms-dreaming-enqueue (hourly)",
    "news": "qms-news-enqueue (daily 06:00 UTC) → ReadingSummaryHandler",
    "file_processing": "on-demand (file upload)",
    "scheduled": "on-demand (kv_rebuild, embedding_backfill)",
}

# Module-level flag set by the callback or per-command --local
_use_local: bool = False


def _set_local(local: bool) -> None:
    """Set the local flag from either the callback or a subcommand option."""
    global _use_local
    if local:
        _use_local = True


# ── Shared helpers ────────────────────────────────────────────────────────────


def _check_port(port: int, host: str = "localhost", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _db_connect_error(exc: Exception, *, local: bool) -> None:
    """Pretty-print a DB connection failure with actionable hints, then exit."""
    _con.print(f"[red bold]Database connection failed:[/red bold] {exc}")
    _con.print()
    if local:
        _con.print("Check that your local Postgres container is healthy:")
        _con.print("  docker compose ps")
        _con.print("  docker compose logs postgres")
    else:
        _con.print("The remote port-forward is open but the connection was rejected.")
        _con.print("Check that P8_DATABASE_URL has the correct credentials for the remote DB.")
        _con.print()
        _con.print("Port-forward commands:")
        _con.print(f"  kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw {REMOTE_PG_PORT}:5432 &")
        _con.print(f"  kubectl --context=p8-w-1 -n p8 port-forward svc/openbao {REMOTE_VAULT_PORT}:8200 &")
        _con.print()
        _con.print("Or use [bold]--local[/bold] to target the local docker-compose DB instead.")
    raise typer.Exit(1)


def _fetch_remote_db_password() -> str:
    """Read the remote DB password from the k8s secret, or prompt."""
    import subprocess

    try:
        result = subprocess.run(
            [
                "kubectl", "--context=p8-w-1", "-n", "p8",
                "get", "secret", "p8-database-credentials",
                "-o", "jsonpath={.data.password}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            import base64
            return base64.b64decode(result.stdout.strip()).decode()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    _con.print("[yellow]Could not read remote DB password from k8s secret.[/yellow]")
    _con.print("Set P8_DATABASE_URL with remote credentials, e.g.:")
    _con.print(f"  export P8_DATABASE_URL=postgresql://{REMOTE_DB_USER}:PASSWORD@localhost:{REMOTE_PG_PORT}/{REMOTE_DB_NAME}")
    raise typer.Exit(1)


@asynccontextmanager
async def _admin_services():
    """Bootstrap services targeting remote (default) or local DB.

    Remote: overrides P8_DATABASE_URL to localhost:5491 (port-forwarded).
    Local:  uses standard settings (localhost:5489).
    """
    if _use_local:
        if not _check_port(LOCAL_PG_PORT):
            _con.print(f"[red]Local Postgres not reachable on port {LOCAL_PG_PORT}[/red]")
            _con.print("Start it: docker compose up -d")
            raise typer.Exit(1)
        try:
            ctx = _svc.bootstrap_services()
            svc = await ctx.__aenter__()
        except Exception as exc:
            _db_connect_error(exc, local=True)
        try:
            yield svc
        finally:
            await ctx.__aexit__(None, None, None)
    else:
        if not _check_port(REMOTE_PG_PORT):
            _con.print(f"[red bold]Remote Postgres not reachable on localhost:{REMOTE_PG_PORT}[/red bold]")
            _con.print()
            _con.print("Open the port-forward first:")
            _con.print(f"  kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw {REMOTE_PG_PORT}:5432 &")
            _con.print(f"  kubectl --context=p8-w-1 -n p8 port-forward svc/openbao {REMOTE_VAULT_PORT}:8200 &")
            _con.print()
            _con.print("Or use [bold]--local[/bold] for the local docker-compose DB.")
            raise typer.Exit(1)

        # Rewrite DB URL to point at port-forwarded remote.
        # If the current URL is the local dev default, build a proper remote URL
        # with the correct user/dbname. Fetch the password from the k8s secret.
        # If user already set a custom URL (e.g. with real creds), respect it.
        from p8.settings import get_settings
        current_url = os.environ.get("P8_DATABASE_URL", get_settings().database_url)
        old_url = os.environ.get("P8_DATABASE_URL")

        if f":{LOCAL_PG_PORT}" in current_url:
            # Local dev URL — need to swap user, password, port, and dbname
            remote_password = _fetch_remote_db_password()
            os.environ["P8_DATABASE_URL"] = (
                f"postgresql://{REMOTE_DB_USER}:{remote_password}"
                f"@localhost:{REMOTE_PG_PORT}/{REMOTE_DB_NAME}"
            )

        # Hint vault if port-forward is up
        if _check_port(REMOTE_VAULT_PORT) and not os.environ.get("P8_KMS_VAULT_URL"):
            os.environ.setdefault("P8_KMS_PROVIDER", "vault")
            os.environ["P8_KMS_VAULT_URL"] = f"http://localhost:{REMOTE_VAULT_PORT}"

        try:
            ctx = _svc.bootstrap_services()
            svc = await ctx.__aenter__()
        except Exception as exc:
            _db_connect_error(exc, local=False)
        try:
            yield svc
        finally:
            await ctx.__aexit__(None, None, None)
            if old_url is not None:
                os.environ["P8_DATABASE_URL"] = old_url
            elif "P8_DATABASE_URL" in os.environ:
                del os.environ["P8_DATABASE_URL"]


@admin_app.callback()
def _admin_callback(
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """Admin tools for the p8 processing pipeline. Defaults to remote (Hetzner via port-forward)."""
    global _use_local
    _use_local = local


def _run(coro):
    asyncio.run(coro)


def _short(val, n: int = 8) -> str:
    return str(val)[:n] if val else "-"


def _ts(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return dt.strftime(fmt) if dt else "-"


def _age(dt: datetime | None) -> str:
    """Human-readable age like '2h ago' or '3d ago'."""
    if not dt:
        return "-"
    from datetime import timezone
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        from datetime import timezone as tz
        dt = dt.replace(tzinfo=tz.utc)
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _status_style(status: str) -> str:
    return {
        "pending": "yellow",
        "processing": "blue",
        "completed": "green",
        "failed": "red bold",
    }.get(status, "")


def _status_text(status: str) -> Text:
    return Text(status, style=_status_style(status))


def _pct_bar(used: int, limit: int, width: int = 15) -> Text:
    if limit <= 0:
        return Text("?" * width, style="dim")
    ratio = min(used / limit, 1.0)
    filled = int(ratio * width)
    style = "green" if ratio < 0.7 else ("yellow" if ratio < 0.9 else "red bold")
    bar = Text("")
    bar.append("#" * filled, style=style)
    bar.append("." * (width - filled), style="dim")
    bar.append(f" {ratio:>5.0%}", style=style)
    return bar


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / (1024**3):.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / (1024**2):.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


async def _resolve_user(db, uid: UUID) -> tuple[str, str]:
    """Look up user name + email by id or user_id."""
    row = await db.fetchrow(
        "SELECT name, email FROM users WHERE id = $1 OR user_id = $1 LIMIT 1", uid
    )
    if row:
        return (row["name"] or _short(uid, 12)), (row["email"] or "")
    return _short(uid, 12), ""


# ── Health ────────────────────────────────────────────────────────────────────


async def _health(user_email: str | None, user_id: UUID | None):
    """Per-user task health: what's due, what ran, what's stuck."""
    async with _admin_services() as (db, enc, _settings, *_rest):
        # Resolve target users
        if user_email:
            # Find user by encrypted email match
            all_users = await db.fetch(
                "SELECT id, user_id, name, email FROM users WHERE deleted_at IS NULL"
            )
            matched = []
            for u in all_users:
                email = u["email"] or ""
                if enc and email:
                    try:
                        email = await enc.decrypt(email)
                    except Exception:
                        pass
                if user_email.lower() in email.lower():
                    matched.append({**dict(u), "_email": email})
            if not matched:
                _con.print(f"No user matching '{user_email}'", style="red")
                return
            users = matched
        elif user_id:
            row = await db.fetchrow(
                "SELECT id, user_id, name, email FROM users "
                "WHERE (id = $1 OR user_id = $1) AND deleted_at IS NULL LIMIT 1",
                user_id,
            )
            if not row:
                _con.print(f"User {user_id} not found", style="red")
                return
            email = row["email"] or ""
            if enc and email:
                try:
                    email = await enc.decrypt(email)
                except Exception:
                    email = str(row["id"])[:12]
            users = [{**dict(row), "_email": email}]
        else:
            # All active users
            all_users = await db.fetch(
                "SELECT id, user_id, name, email FROM users WHERE deleted_at IS NULL ORDER BY name"
            )
            users = []
            for u in all_users:
                email = u["email"] or ""
                if enc and email:
                    try:
                        email = await enc.decrypt(email)
                    except Exception:
                        email = ""
                users.append({**dict(u), "_email": email})

        # ── Pipeline check: pg_cron → pg_net → task_queue → KEDA → worker ──
        _con.print()
        pipe = Table(title="Processing Pipeline", title_style="bold", show_lines=True)
        pipe.add_column("Stage", min_width=16)
        pipe.add_column("Status")
        pipe.add_column("Detail", min_width=50)

        # 0. pg_net / GUC: is the internal API URL set?
        internal_url = await db.fetchval(
            "SELECT current_setting('p8.internal_api_url', true)"
        )
        if internal_url:
            pipe.add_row("pg_net GUC", Text("ok", style="green"),
                         f"p8.internal_api_url = {internal_url}")
        else:
            pipe.add_row("pg_net GUC", Text("MISSING", style="red bold"),
                         "p8.internal_api_url not set — pg_cron HTTP jobs will fail\n"
                         "Fix: ALTER DATABASE p8db SET p8.internal_api_url = 'http://p8-api.p8.svc:8000';")

        # Check for reminder jobs with hardcoded URLs (legacy)
        stale_jobs = await db.fetch(
            "SELECT jobname FROM cron.job "
            "WHERE jobname LIKE 'reminder-%' "
            "AND command NOT LIKE '%current_setting%internal_api_url%'"
        )
        if stale_jobs:
            names = ", ".join(r["jobname"][:20] for r in stale_jobs[:5])
            pipe.add_row("pg_net jobs", Text("STALE", style="red bold"),
                         f"{len(stale_jobs)} reminder job(s) hardcode a URL instead of using GUC: {names}\n"
                         "Fix: restart API to trigger self-heal, or run p8 admin heal-jobs")
        else:
            # Check for recent reminder failures
            fail_count = await db.fetchval(
                "SELECT COUNT(*) FROM cron.job j "
                "JOIN cron.job_run_details d ON d.jobid = j.jobid "
                "WHERE j.jobname LIKE 'reminder-%' AND d.status = 'failed' "
                "AND d.start_time > CURRENT_TIMESTAMP - INTERVAL '24 hours'"
            )
            if fail_count and fail_count > 0:
                pipe.add_row("pg_net jobs", Text("FAILING", style="red bold"),
                             f"{fail_count} reminder failure(s) in last 24h — check pg_cron Jobs table below")
            else:
                pipe.add_row("pg_net jobs", Text("ok", style="green"), "all reminder jobs use GUC-based URLs")

        # 1. pg_cron: are enqueue jobs running?
        cron_ok = True
        for job_name in ("qms-dreaming-enqueue", "qms-news-enqueue"):
            row = await db.fetchrow(
                "SELECT j.active, d.status, d.start_time, d.return_message "
                "FROM cron.job j LEFT JOIN LATERAL ("
                "  SELECT status, start_time, return_message "
                "  FROM cron.job_run_details WHERE jobid = j.jobid "
                "  ORDER BY start_time DESC LIMIT 1"
                ") d ON true WHERE j.jobname = $1",
                job_name,
            )
            if not row:
                pipe.add_row("pg_cron", _status_text("failed"), f"{job_name}: JOB MISSING from cron.job")
                cron_ok = False
            elif not row["active"]:
                pipe.add_row("pg_cron", _status_text("failed"), f"{job_name}: INACTIVE")
                cron_ok = False
            elif row["status"] and row["status"] != "succeeded":
                pipe.add_row("pg_cron", _status_text("failed"),
                             f"{job_name}: last run {row['status']} — {(row['return_message'] or '')[:40]}")
                cron_ok = False
        if cron_ok:
            pipe.add_row("pg_cron", Text("ok", style="green"), "enqueue jobs active and succeeding")

        # 2. task_queue: are there pending tasks?
        pending_counts = await db.fetch(
            "SELECT tier, COUNT(*) AS cnt FROM task_queue "
            "WHERE status = 'pending' AND scheduled_at <= CURRENT_TIMESTAMP "
            "GROUP BY tier ORDER BY tier"
        )
        if pending_counts:
            parts = [f"{r['tier']}={r['cnt']}" for r in pending_counts]
            pipe.add_row("task_queue", Text("pending", style="yellow"), f"due now: {', '.join(parts)}")
        else:
            pipe.add_row("task_queue", Text("empty", style="green"), "no overdue tasks")

        # 3. KEDA → Workers: have any workers ever claimed?
        worker_rows = await db.fetch(
            "SELECT claimed_by, MAX(claimed_at) AS last_claim, COUNT(*) AS cnt "
            "FROM task_queue WHERE claimed_by IS NOT NULL "
            "GROUP BY claimed_by"
        )
        if worker_rows:
            from datetime import timezone as _tz
            _now = datetime.now(_tz.utc)
            recent = [w for w in worker_rows
                      if w["last_claim"] and (_now - w["last_claim"].replace(tzinfo=_tz.utc
                          if w["last_claim"].tzinfo is None else w["last_claim"].tzinfo)).total_seconds() < 3600]
            stale = [w for w in worker_rows if w not in recent]
            if recent:
                parts = [f"{w['claimed_by']} ({_age(w['last_claim'])})" for w in recent]
                pipe.add_row("workers", Text("ok", style="green"), f"active: {', '.join(parts)}")
            if stale:
                parts = [f"{w['claimed_by']} ({_age(w['last_claim'])})" for w in stale]
                pipe.add_row("workers", Text("stale", style="yellow"),
                             f"last seen >1h ago: {', '.join(parts)}")
        else:
            pipe.add_row(
                "workers",
                Text("NONE", style="red bold"),
                "no task has ever been claimed\n"
                "Pipeline: pg_cron enqueues → KEDA polls task_queue → scales worker deployment 0→N → worker claims\n"
                "Check: kubectl get scaledobject -n p8  (KEDA trigger exists?)\n"
                "Check: kubectl get TriggerAuthentication -n p8  (p8-keda-pg-auth can reach DB?)\n"
                "Check: kubectl logs -n keda -l app=keda-operator  (KEDA errors?)\n"
                "Local dev: python -m p8.workers.processor --tier small",
            )

        _con.print(pipe)

        # Per-user health
        for u in users:
            effective_uid = u["user_id"] or u["id"]
            name = u["name"] or _short(u["id"], 12)
            email = u["_email"]

            table = Table(
                title=f"{name} ({email})" if email else name,
                title_style="bold cyan",
                show_lines=True,
            )
            table.add_column("Task Type", min_width=18)
            table.add_column("Source", style="dim")
            table.add_column("Pending", min_width=14)
            table.add_column("Last Run", min_width=20)
            table.add_column("Status")
            table.add_column("Issues", min_width=30)

            for task_type, source in _TASK_TYPES.items():
                # Pending tasks for this user + type
                pending = await db.fetch(
                    "SELECT id, scheduled_at, created_at, retry_count "
                    "FROM task_queue "
                    "WHERE user_id = $1 AND task_type = $2 AND status = 'pending' "
                    "ORDER BY scheduled_at ASC",
                    effective_uid, task_type,
                )

                # Last completed or failed
                last = await db.fetchrow(
                    "SELECT status, completed_at, error, created_at, result "
                    "FROM task_queue "
                    "WHERE user_id = $1 AND task_type = $2 AND status IN ('completed', 'failed') "
                    "ORDER BY COALESCE(completed_at, created_at) DESC LIMIT 1",
                    effective_uid, task_type,
                )

                # Currently processing?
                processing = await db.fetchrow(
                    "SELECT id, claimed_at, claimed_by "
                    "FROM task_queue "
                    "WHERE user_id = $1 AND task_type = $2 AND status = 'processing' "
                    "LIMIT 1",
                    effective_uid, task_type,
                )

                # Build pending cell
                if pending:
                    p = pending[0]
                    pending_str = f"{len(pending)} task(s)\ndue {_ts(p['scheduled_at'])}\n({_age(p['scheduled_at'])})"
                elif processing:
                    pending_str = f"processing\nby {_short(processing['claimed_by'], 12)}"
                else:
                    pending_str = "-"

                # Build last run cell
                if last:
                    last_str = f"{_ts(last['completed_at'])}\n({_age(last['completed_at'])})"
                    last_status = _status_text(last["status"])
                else:
                    last_str = "never"
                    last_status = Text("never", style="dim")

                # Diagnose issues
                issues: list[str] = []

                if pending and not worker_rows:
                    issues.append("NO WORKERS — will never be claimed")

                if pending:
                    oldest = pending[0]
                    from datetime import timezone
                    now = datetime.now(timezone.utc)
                    sched = oldest["scheduled_at"]
                    if sched and sched.tzinfo is None:
                        sched = sched.replace(tzinfo=timezone.utc)
                    if sched and (now - sched).total_seconds() > 3600:
                        hours = (now - sched).total_seconds() / 3600
                        issues.append(f"OVERDUE by {hours:.0f}h")
                    if oldest["retry_count"] > 0:
                        issues.append(f"retried {oldest['retry_count']}x")

                if last and last["status"] == "failed":
                    err = (last["error"] or "unknown")[:50]
                    issues.append(f"last failed: {err}")

                if processing:
                    claimed = processing["claimed_at"]
                    if claimed:
                        if claimed.tzinfo is None:
                            claimed = claimed.replace(tzinfo=timezone.utc)
                        stuck_mins = (datetime.now(timezone.utc) - claimed).total_seconds() / 60
                        if stuck_mins > 15:
                            issues.append(f"STUCK processing for {stuck_mins:.0f}m")

                if not pending and not last and not processing:
                    if task_type in ("dreaming", "news"):
                        issues.append("never scheduled — check cron / user activity")

                issue_text = Text()
                for i, iss in enumerate(issues):
                    if i > 0:
                        issue_text.append("\n")
                    style = "red bold" if any(w in iss for w in ("NO WORKER", "OVERDUE", "STUCK", "failed")) else "yellow"
                    issue_text.append(iss, style=style)

                table.add_row(
                    task_type,
                    source,
                    pending_str,
                    last_str,
                    last_status,
                    issue_text if issues else Text("ok", style="green"),
                )

            _con.print(table)

        # pg_cron health
        _con.print()
        cron_table = Table(title="pg_cron Jobs", title_style="bold")
        cron_table.add_column("Job")
        cron_table.add_column("Schedule")
        cron_table.add_column("Active")
        cron_table.add_column("Last Run")
        cron_table.add_column("Status")
        cron_table.add_column("Result")

        jobs = await db.fetch("SELECT jobid, jobname, schedule, active FROM cron.job ORDER BY jobname")
        for j in jobs:
            last_run = await db.fetchrow(
                "SELECT status, return_message, start_time "
                "FROM cron.job_run_details WHERE jobid = $1 "
                "ORDER BY start_time DESC LIMIT 1",
                j["jobid"],
            )
            active_style = "green" if j["active"] else "red"
            if last_run:
                run_status = "succeeded" if last_run["status"] == "succeeded" else last_run["status"]
                run_style = "green" if run_status == "succeeded" else "red"
                cron_table.add_row(
                    j["jobname"],
                    j["schedule"],
                    Text("yes" if j["active"] else "NO", style=active_style),
                    f"{_ts(last_run['start_time'])} ({_age(last_run['start_time'])})",
                    Text(run_status, style=run_style),
                    (last_run["return_message"] or "")[:40],
                )
            else:
                cron_table.add_row(
                    j["jobname"],
                    j["schedule"],
                    Text("yes" if j["active"] else "NO", style=active_style),
                    "never",
                    Text("-", style="dim"),
                    "",
                )

        _con.print(cron_table)


@admin_app.command()
def health(
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Filter by user email (partial match)"),
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="Filter by user UUID"),
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """Task health per user — what's due, what ran, what's stuck, and why."""
    _set_local(local)
    _run(_health(email, UUID(user_id) if user_id else None))


# ── Queue ─────────────────────────────────────────────────────────────────────


async def _queue_aggregate(status: str, limit: int):
    async with _admin_services() as (db, _enc, _settings, *_rest):
        rows = await db.fetch(
            "SELECT tenant_id, task_type, COUNT(*) AS cnt, "
            "       MIN(scheduled_at) AS earliest, MAX(scheduled_at) AS latest "
            "FROM task_queue WHERE status = $1 "
            "GROUP BY tenant_id, task_type "
            "ORDER BY cnt DESC",
            status,
        )
        if not rows:
            _con.print(f"No {status} tasks.", style="dim")
            return

        table = Table(title=f"{status.upper()} Tasks (aggregate)", title_style="bold")
        table.add_column("Tenant", max_width=24)
        table.add_column("Type")
        table.add_column("Count", justify="right")
        table.add_column("Earliest")
        table.add_column("Latest")
        table.add_column("Age", style="dim")

        for i, r in enumerate(rows):
            if i >= limit:
                table.add_row("", f"... {len(rows) - limit} more groups", "", "", "", "", style="dim")
                break
            table.add_row(
                _short(r["tenant_id"], 20) if r["tenant_id"] else "(system)",
                r["task_type"],
                str(r["cnt"]),
                _ts(r["earliest"]),
                _ts(r["latest"]),
                _age(r["earliest"]),
            )

        _con.print(table)
        _con.print(f"Total {status}: {sum(r['cnt'] for r in rows)}", style="bold")


async def _queue_detail(status: str, task_type: str | None, limit: int, offset: int):
    async with _admin_services() as (db, _enc, _settings, *_rest):
        where = "WHERE status = $1"
        params: list = [status]
        if task_type:
            params.append(task_type)
            where += f" AND task_type = ${len(params)}"

        rows = await db.fetch(
            f"SELECT id, task_type, tier, tenant_id, user_id, priority, "
            f"       scheduled_at, retry_count, error, created_at "
            f"FROM task_queue {where} "
            f"ORDER BY scheduled_at ASC "
            f"LIMIT ${len(params)+1} OFFSET ${len(params)+2}",
            *params, limit, offset,
        )
        total = await db.fetchval(
            f"SELECT COUNT(*) FROM task_queue {where}", *params
        )

        if not rows:
            _con.print(f"No {status} tasks found.", style="dim")
            return

        table = Table(title=f"{status.upper()} Tasks (detail)", title_style="bold")
        table.add_column("ID", max_width=10)
        table.add_column("Type")
        table.add_column("Tier")
        table.add_column("Tenant", max_width=14)
        table.add_column("User", max_width=10)
        table.add_column("Pri", justify="right")
        table.add_column("Retries", justify="right")
        table.add_column("Scheduled")
        table.add_column("Age", style="dim")
        if status == "failed":
            table.add_column("Error", max_width=40)

        for r in rows:
            row_data = [
                _short(r["id"]),
                r["task_type"],
                r["tier"],
                _short(r["tenant_id"], 12) if r["tenant_id"] else "-",
                _short(r["user_id"]),
                str(r["priority"]),
                str(r["retry_count"]),
                _ts(r["scheduled_at"]),
                _age(r["scheduled_at"]),
            ]
            if status == "failed":
                row_data.append((r["error"] or "")[:40])
            table.add_row(*row_data)

        _con.print(table)
        _con.print(f"Showing {offset+1}-{offset+len(rows)} of {total}", style="dim")


@admin_app.command()
def queue(
    detail: bool = typer.Option(False, "--detail", "-d", help="Show individual tasks instead of aggregate"),
    status: str = typer.Option("pending", "--status", "-s", help="Task status: pending, failed, processing, completed"),
    task_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by task_type"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max groups (aggregate) or rows (detail)"),
    offset: int = typer.Option(0, "--offset", "-o", help="Pagination offset (detail mode)"),
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """Queue tasks — aggregate by tenant or paginated detail. Use --status to filter."""
    _set_local(local)
    if detail:
        _run(_queue_detail(status, task_type, limit, offset))
    else:
        _run(_queue_aggregate(status, limit))


# ── Quota ─────────────────────────────────────────────────────────────────────


async def _quota_report(user_id: UUID | None, limit: int):
    from p8.services.usage import get_all_usage, get_limits, get_user_plan

    async with _admin_services() as (db, enc, _settings, *_rest):
        if user_id:
            user_rows = [{"uid": user_id}]
        else:
            user_rows = await db.fetch(
                "SELECT id AS uid FROM users "
                "WHERE deleted_at IS NULL ORDER BY id LIMIT $1",
                limit,
            )
        if not user_rows:
            _con.print("No users found.", style="dim")
            return

        byte_resources = {"storage_bytes"}
        resource_keys = ["chat_tokens", "storage_bytes", "dreaming_minutes", "web_searches_daily"]

        table = Table(title="Quota Report", title_style="bold")
        table.add_column("User", min_width=18)
        table.add_column("Plan")
        for k in resource_keys:
            table.add_column(k.replace("_", " ").title(), min_width=16)

        for row in user_rows:
            uid = row["uid"]
            plan_id = await get_user_plan(db, uid)
            usage = await get_all_usage(db, uid, plan_id)

            name, email = await _resolve_user(db, uid)
            if email and enc:
                try:
                    email = await enc.decrypt(email)
                except Exception:
                    email = ""
            display = f"{name}\n{email}" if email else name

            cells: list[Text | str] = [display, plan_id]
            for key in resource_keys:
                info = usage.get(key)
                if not info:
                    cells.append("-")
                    continue
                used, lim = info["used"], info["limit"]
                bar = _pct_bar(used, lim)
                if key in byte_resources:
                    bar.append(f"\n{_fmt_bytes(used)}/{_fmt_bytes(lim)}", style="dim")
                else:
                    bar.append(f"\n{used:,}/{lim:,}", style="dim")
                cells.append(bar)
            table.add_row(*cells)

        _con.print(table)


async def _quota_reset(user_id: UUID, resource_type: str | None):
    async with _admin_services() as (db, _enc, _settings, *_rest):
        if resource_type:
            await db.execute(
                "DELETE FROM usage_tracking WHERE user_id = $1 AND resource_type = $2 "
                "AND period_start = date_trunc('month', CURRENT_DATE)::date",
                user_id, resource_type,
            )
            _con.print(f"Reset [bold]{resource_type}[/bold] for user {_short(user_id)}")
        else:
            await db.execute(
                "DELETE FROM usage_tracking WHERE user_id = $1 "
                "AND period_start >= date_trunc('month', CURRENT_DATE)::date",
                user_id,
            )
            _con.print(f"Reset [bold]all[/bold] current-period quotas for user {_short(user_id)}")


@admin_app.command()
def quota(
    user_id: Optional[str] = typer.Option(None, "--user", "-u", help="Filter to a single user UUID"),
    reset: bool = typer.Option(False, "--reset", "-r", help="Reset quotas instead of reporting"),
    resource: Optional[str] = typer.Option(None, "--resource", help="Specific resource to reset (with --reset)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max users to show"),
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """User quota report — utilization percentage per resource, or reset quotas."""
    _set_local(local)
    if reset:
        if not user_id:
            _con.print("--user is required with --reset", style="red")
            raise typer.Exit(1)
        _run(_quota_reset(UUID(user_id), resource))
    else:
        _run(_quota_report(UUID(user_id) if user_id else None, limit))


# ── Enqueue ──────────────────────────────────────────────────────────────────


_VALID_TASK_TYPES = ("dreaming", "news", "reading_summary", "file_processing", "scheduled")


async def _enqueue_task(user_id: UUID, task_type: str, delay_minutes: int, payload: dict | None):
    import json as _json

    async with _admin_services() as (db, enc, _settings, *_rest):
        name, email = await _resolve_user(db, user_id)
        if enc and email:
            try:
                email = await enc.decrypt(email)
            except Exception:
                email = ""

        from datetime import timezone, timedelta
        scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

        row = await db.fetchrow(
            "INSERT INTO task_queue (task_type, tier, user_id, payload, status, priority, scheduled_at, max_retries) "
            "VALUES ($1, 'small', $2, $3, 'pending', 0, $4, 3) "
            "RETURNING id, scheduled_at",
            task_type, user_id,
            _json.dumps(payload or {"source": "admin-cli"}),
            scheduled_at,
        )

        _con.print(f"[green bold]Enqueued[/green bold] {task_type}")
        _con.print(f"  task_id:      {row['id']}")
        _con.print(f"  user:         {name} ({email})" if email else f"  user:         {name}")
        _con.print(f"  scheduled_at: {_ts(row['scheduled_at'])} ({delay_minutes}m from now)")
        _con.print(f"  tier:         small")


@admin_app.command()
def enqueue(
    task_type: str = typer.Argument(help=f"Task type: {', '.join(_VALID_TASK_TYPES)}"),
    user_id: str = typer.Option(..., "--user", "-u", help="User UUID"),
    delay: int = typer.Option(0, "--delay", "-d", help="Minutes to delay before task is due"),
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """Enqueue a one-off task for a user (reading_summary, dreaming, news, etc.)."""
    _set_local(local)
    if task_type not in _VALID_TASK_TYPES:
        _con.print(f"[red]Invalid task type '{task_type}'. Choose from: {', '.join(_VALID_TASK_TYPES)}[/red]")
        raise typer.Exit(1)
    _run(_enqueue_task(UUID(user_id), task_type, delay, None))


# ── Heal Jobs ────────────────────────────────────────────────────────────────


async def _heal_jobs():
    async with _admin_services() as (db, _enc, _settings, *_rest):
        from p8.api.main import _heal_reminder_jobs

        await _heal_reminder_jobs(db)
        _con.print("[green]Reminder jobs healed[/green]")


@admin_app.command("heal-jobs")
def heal_jobs(
    local: bool = typer.Option(False, "--local", "-L", help="Target local docker-compose DB instead of remote"),
):
    """Fix reminder cron jobs that hardcode a URL instead of using the GUC."""
    _set_local(local)
    _run(_heal_jobs())


# ── Env ──────────────────────────────────────────────────────────────────────


def _parse_env_keys(env_path: str) -> list[str]:
    """Parse keys from a .env file, skipping comments and blank lines."""
    keys = []
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key = line.split("=", 1)[0].strip()
            if key:
                keys.append(key)
    return keys


def _parse_kustomization_literals(path: str) -> list[str]:
    """Parse configMapGenerator literal keys from a kustomization.yaml."""
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return []
    keys = []
    in_literals = False
    for line in p.read_text().splitlines():
        if "literals:" in line and not line.strip().startswith("#"):
            in_literals = True
            continue
        if in_literals:
            stripped = line.strip()
            if stripped.startswith("- "):
                item = stripped[2:].split("=", 1)[0].strip().split("#")[0].strip()
                if item:
                    keys.append(item)
            elif stripped.startswith("#"):
                continue
            elif stripped:
                in_literals = False
    return keys


def _parse_secret_keys(path: str) -> list[str]:
    """Parse p8-app-secrets stringData keys from secrets.yaml."""
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return []
    keys = []
    in_app_secrets = False
    in_stringdata = False
    for line in p.read_text().splitlines():
        if "name:" in line and "p8-app-secrets" in line:
            in_app_secrets = True
            continue
        if line.strip() == "---":
            in_app_secrets = False
            in_stringdata = False
            continue
        if in_app_secrets and line.startswith("stringData:"):
            in_stringdata = True
            continue
        if in_stringdata:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if line[0].isalpha():
                in_stringdata = False
                in_app_secrets = False
                continue
            key = stripped.split(":")[0].strip()
            if key:
                keys.append(key)
    return keys


@admin_app.command("env")
def env_check():
    """Validate that every .env key is covered by K8s manifests."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[3]

    env_file = repo_root / ".env"
    if not env_file.exists():
        env_file = repo_root / ".env.example"
    if not env_file.exists():
        _con.print("[red]No .env or .env.example found at repo root[/red]")
        raise typer.Exit(2)

    _con.print(f"Using env file: {env_file.relative_to(repo_root)}")
    env_keys = _parse_env_keys(str(env_file))
    _con.print(f"Found {len(env_keys)} keys in env file")

    base_kust = repo_root / "manifests/application/p8-stack/base/kustomization.yaml"
    hetzner_kust = repo_root / "manifests/application/p8-stack/overlays/hetzner/kustomization.yaml"
    secrets_file = repo_root / "manifests/application/p8-stack/overlays/hetzner/secrets.yaml"

    configmap_keys = _parse_kustomization_literals(str(base_kust)) + _parse_kustomization_literals(str(hetzner_kust))
    _con.print(f"Found {len(configmap_keys)} configMap literals")

    secret_keys = _parse_secret_keys(str(secrets_file))
    _con.print(f"Found {len(secret_keys)} secret keys in p8-app-secrets")

    # Dev-only keys intentionally not in K8s
    dev_only = {"P8_DATABASE_URL", "P8_KMS_LOCAL_KEYFILE"}
    covered = set(configmap_keys) | set(secret_keys)

    gaps = []
    skipped = []
    for key in env_keys:
        if key in dev_only:
            skipped.append(key)
        elif key not in covered:
            gaps.append(key)

    _con.print()
    if skipped:
        _con.print(f"Skipped {len(skipped)} dev-only key(s): {', '.join(skipped)}")

    if not gaps:
        _con.print("[green]All env keys are covered by K8s manifests.[/green]")
    else:
        _con.print(f"[red bold]GAPS: {len(gaps)} env key(s) not found in configMap or secrets:[/red bold]")
        for g in gaps:
            _con.print(f"  - {g}")
        raise typer.Exit(1)


# ── Sync Secrets ─────────────────────────────────────────────────────────────


@admin_app.command("sync-secrets")
def sync_secrets(
    bao_addr: str = typer.Option("http://127.0.0.1:8200", "--addr", "-a", envvar="BAO_ADDR",
                                  help="OpenBao address"),
    bao_token: Optional[str] = typer.Option(None, "--token", "-t", envvar="BAO_TOKEN",
                                             help="OpenBao token (falls back to VAULT_TOKEN)"),
):
    """Sync p8-app-secrets from .env into OpenBao KV v2."""
    import pathlib
    import shutil
    import subprocess

    repo_root = pathlib.Path(__file__).resolve().parents[3]

    token = bao_token or os.environ.get("VAULT_TOKEN", "")
    if not token:
        _con.print("[red]BAO_TOKEN (or VAULT_TOKEN) must be set[/red]")
        raise typer.Exit(1)

    env_file = repo_root / ".env"
    if not env_file.exists():
        _con.print("[red]No .env found at repo root[/red]")
        raise typer.Exit(1)

    secrets_file = repo_root / "manifests/application/p8-stack/overlays/hetzner/secrets.yaml"
    allowed_keys = set(_parse_secret_keys(str(secrets_file)))
    _con.print(f"Allowed keys from p8-app-secrets: {len(allowed_keys)}")

    # Read .env and collect matching key=value pairs
    kv_pairs = []
    kv_dict = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in allowed_keys:
            kv_pairs.append(f"{key}={value}")
            kv_dict[key] = value

    if not kv_pairs:
        _con.print("No matching keys found in .env — nothing to sync.")
        return

    _con.print(f"Syncing {len(kv_pairs)} key(s) to OpenBao at {bao_addr}...")

    use_cli = shutil.which("bao") is not None

    if use_cli:
        _con.print("Using: bao CLI")
        env = {**os.environ, "BAO_ADDR": bao_addr, "BAO_TOKEN": token}
        subprocess.run(["bao", "kv", "put", "secret/p8/app-secrets"] + kv_pairs,
                        env=env, check=True)
    else:
        import json as _json
        import urllib.request
        import urllib.error

        _con.print("Using: HTTP API (bao CLI not found)")

        # Enable KV v2
        try:
            req = urllib.request.Request(
                f"{bao_addr}/v1/sys/mounts/secret",
                data=_json.dumps({"type": "kv", "options": {"version": "2"}}).encode(),
                headers={"X-Vault-Token": token, "Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req)
            _con.print("Enabled KV v2 engine at secret/")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                _con.print("KV v2 engine already mounted at secret/")
            else:
                _con.print(f"Mount check returned HTTP {e.code} (continuing)")

        # Write secrets
        payload = _json.dumps({"data": kv_dict}).encode()
        req = urllib.request.Request(
            f"{bao_addr}/v1/secret/data/p8/app-secrets",
            data=payload,
            headers={"X-Vault-Token": token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            _con.print(f"[red]OpenBao returned HTTP {e.code}[/red]")
            raise typer.Exit(1)

    _con.print(f"[green]Done. Wrote {len(kv_pairs)} key(s) to secret/p8/app-secrets[/green]")
