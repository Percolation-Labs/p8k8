"""p8 db — compare local and remote database schemas, generate migrations.

Connects to both a local and remote PostgreSQL database and reports
schema differences: tables, columns, types, indexes, functions, triggers.

Assumes the remote database is available via port-forward
(e.g. kubectl port-forward svc/postgres 5433:5432). Raises a clear
error when the remote port is not reachable.
"""

from __future__ import annotations

import asyncio
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

db_app = typer.Typer(no_args_is_help=True)

# Default remote port when rewriting the local URL for port-forward access
_DEFAULT_REMOTE_PORT = 5433

# Instance-specific tables/functions that differ by environment but aren't real drift.
# These are excluded from diff output and migration generation.
_IGNORE_TABLES = {
    "cron.job",              # pg_cron internal
    "cron.job_run_details",  # pg_cron internal
}
_IGNORE_FUNCTIONS = {
    "seed_table_schemas",    # contains hardcoded schema list per instance
}
_IGNORE_INDEX_PREFIXES = (
    "pg_",                   # system catalog indexes
)
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "sql" / "migrations"


def _parse_dsn(url: str) -> dict:
    """Parse a PostgreSQL DSN into components."""
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "user": p.username or "p8",
        "password": p.password or "",
        "database": p.path.lstrip("/") or "p8",
    }


def _check_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if a TCP connection succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _local_to_remote_url(local_url: str, remote_port: int) -> str:
    """Derive a remote URL from the local one, changing only the port."""
    p = urlparse(local_url)
    netloc = f"{p.username}"
    if p.password:
        netloc += f":{p.password}"
    netloc += f"@{p.hostname or 'localhost'}:{remote_port}"
    return f"{p.scheme}://{netloc}{p.path}"


# ── Introspection queries ──────────────────────────────────────────────


_TABLES_QUERY = """
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;
"""

_COLUMNS_QUERY = """
SELECT table_name, column_name, data_type, udt_name,
       is_nullable, column_default,
       character_maximum_length, numeric_precision
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;
"""

_FULL_TABLE_DDL_QUERY = """
SELECT c.relname AS table_name,
       pg_get_tabledef(c.oid) AS ddl
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname;
"""

# Fallback: reconstruct CREATE TABLE from columns when pg_get_tabledef unavailable
_TABLE_COLUMNS_QUERY = """
SELECT table_name, column_name, udt_name, is_nullable, column_default,
       character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = $1
ORDER BY ordinal_position;
"""

_INDEXES_QUERY = """
SELECT indexname, tablename, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;
"""

_FUNCTIONS_QUERY = """
SELECT p.proname AS name,
       pg_get_function_identity_arguments(p.oid) AS args,
       md5(pg_get_functiondef(p.oid)) AS def_hash,
       pg_get_functiondef(p.oid) AS definition
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.prokind IN ('f', 'p')
ORDER BY p.proname;
"""

_TRIGGERS_QUERY = """
SELECT trigger_name, event_object_table, action_timing, event_manipulation,
       action_statement
FROM information_schema.triggers
WHERE trigger_schema = 'public'
ORDER BY event_object_table, trigger_name;
"""

_TRIGGER_FULL_QUERY = """
SELECT pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND NOT t.tgisinternal
  AND c.relname = $1
  AND t.tgname = $2;
"""

_ROW_COUNTS_QUERY = """
SELECT relname AS table_name,
       n_live_tup AS row_count
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY relname;
"""


# ── Snapshot ───────────────────────────────────────────────────────────


async def _snapshot(dsn: str, label: str = "database") -> dict:
    """Take a schema snapshot from a database."""
    import asyncpg

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        raise ConnectionError(
            f"Cannot connect to {label}: {exc}\n"
            f"If this is the remote database, is the port-forward running?"
        ) from exc
    try:
        tables = {
            r["table_name"] for r in await conn.fetch(_TABLES_QUERY)
            if r["table_name"] not in _IGNORE_TABLES
        }

        columns: dict[str, dict] = {}
        for r in await conn.fetch(_COLUMNS_QUERY):
            if r["table_name"] in _IGNORE_TABLES:
                continue
            key = f"{r['table_name']}.{r['column_name']}"
            columns[key] = dict(r)

        indexes: dict[str, dict] = {}
        for r in await conn.fetch(_INDEXES_QUERY):
            if any(r["indexname"].startswith(p) for p in _IGNORE_INDEX_PREFIXES):
                continue
            indexes[r["indexname"]] = dict(r)

        functions: dict[str, dict] = {}
        for r in await conn.fetch(_FUNCTIONS_QUERY):
            if r["name"] in _IGNORE_FUNCTIONS:
                continue
            sig = f"{r['name']}({r['args']})"
            functions[sig] = {"def_hash": r["def_hash"], "definition": r["definition"]}

        triggers: dict[str, dict] = {}
        for r in await conn.fetch(_TRIGGERS_QUERY):
            key = f"{r['event_object_table']}.{r['trigger_name']}"
            triggers[key] = dict(r)

        # Fetch full trigger DDL for each trigger
        for key, trig in triggers.items():
            table, name = key.split(".", 1)
            row = await conn.fetchrow(_TRIGGER_FULL_QUERY, table, name)
            trig["definition"] = row["definition"] if row else None

        row_counts: dict[str, int] = {}
        for r in await conn.fetch(_ROW_COUNTS_QUERY):
            row_counts[r["table_name"]] = r["row_count"]

        return {
            "tables": tables,
            "columns": columns,
            "indexes": indexes,
            "functions": functions,
            "triggers": triggers,
            "row_counts": row_counts,
            "_conn_dsn": dsn,  # kept for generate to fetch extra DDL
        }
    finally:
        await conn.close()


async def _get_create_table_sql(dsn: str, table_name: str) -> str:
    """Reconstruct a CREATE TABLE statement from information_schema."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_TABLE_COLUMNS_QUERY, table_name)
        if not rows:
            return f"-- Could not retrieve columns for {table_name}\n"

        col_lines = []
        for r in rows:
            typ = r["udt_name"]
            if r["character_maximum_length"]:
                typ += f"({r['character_maximum_length']})"
            nullable = "" if r["is_nullable"] == "YES" else " NOT NULL"
            default = f" DEFAULT {r['column_default']}" if r["column_default"] else ""
            col_lines.append(f"    {r['column_name']} {typ}{nullable}{default}")

        cols = ",\n".join(col_lines)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n{cols}\n);\n"
    finally:
        await conn.close()


# ── Diff logic ──────────────────────────────────────────────────────────


def _diff_snapshots(local: dict, remote: dict) -> tuple[list[str], list[dict]]:
    """Compare two snapshots. Returns (display_lines, structured_ops).

    structured_ops is a list of dicts describing each change for SQL generation.
    """
    lines: list[str] = []
    ops: list[dict] = []

    # ── Tables ──
    local_only = local["tables"] - remote["tables"]
    remote_only = remote["tables"] - local["tables"]
    if local_only:
        lines.append("")
        lines.append("Tables only in LOCAL:")
        for t in sorted(local_only):
            lines.append(f"  + {t}")
            ops.append({"type": "create_table", "table": t})
    if remote_only:
        lines.append("")
        lines.append("Tables only in REMOTE:")
        for t in sorted(remote_only):
            lines.append(f"  - {t}")

    # ── Columns ──
    common_tables = local["tables"] & remote["tables"]
    col_diffs: list[str] = []

    local_cols = local["columns"]
    remote_cols = remote["columns"]
    all_col_keys = set(local_cols) | set(remote_cols)

    for key in sorted(all_col_keys):
        table = key.split(".")[0]
        if table not in common_tables:
            continue
        if key in local_cols and key not in remote_cols:
            col_diffs.append(f"  + {key}  (local only)")
            ops.append({"type": "add_column", "key": key, "col": local_cols[key]})
        elif key in remote_cols and key not in local_cols:
            col_diffs.append(f"  - {key}  (remote only)")
        else:
            lc, rc = local_cols[key], remote_cols[key]
            diffs = []
            if lc["udt_name"] != rc["udt_name"]:
                diffs.append(f"type: {lc['udt_name']} vs {rc['udt_name']}")
            if lc["is_nullable"] != rc["is_nullable"]:
                diffs.append(f"nullable: {lc['is_nullable']} vs {rc['is_nullable']}")
            if (lc["column_default"] or "") != (rc["column_default"] or ""):
                diffs.append(f"default differs")
            if diffs:
                col_diffs.append(f"  ~ {key}  ({', '.join(diffs)})")
                ops.append({"type": "alter_column", "key": key, "local": lc, "remote": rc})

    if col_diffs:
        lines.append("")
        lines.append("Column differences:")
        lines.extend(col_diffs)

    # ── Indexes ──
    local_idx = set(local["indexes"])
    remote_idx = set(remote["indexes"])
    idx_local_only = local_idx - remote_idx
    idx_remote_only = remote_idx - local_idx
    idx_common = local_idx & remote_idx
    idx_diffs: list[str] = []

    if idx_local_only:
        for i in sorted(idx_local_only):
            idx_diffs.append(f"  + {i}  (local only)")
            ops.append({"type": "create_index", "name": i, "indexdef": local["indexes"][i]["indexdef"]})
    if idx_remote_only:
        for i in sorted(idx_remote_only):
            idx_diffs.append(f"  - {i}  (remote only)")
    for i in sorted(idx_common):
        if local["indexes"][i]["indexdef"] != remote["indexes"][i]["indexdef"]:
            idx_diffs.append(f"  ~ {i}  (definition differs)")
            ops.append({"type": "replace_index", "name": i, "indexdef": local["indexes"][i]["indexdef"]})

    if idx_diffs:
        lines.append("")
        lines.append("Index differences:")
        lines.extend(idx_diffs)

    # ── Functions ──
    local_fn = set(local["functions"])
    remote_fn = set(remote["functions"])
    fn_diffs: list[str] = []

    for f in sorted(local_fn - remote_fn):
        fn_diffs.append(f"  + {f}  (local only)")
        ops.append({"type": "create_function", "sig": f, "definition": local["functions"][f]["definition"]})
    for f in sorted(remote_fn - local_fn):
        fn_diffs.append(f"  - {f}  (remote only)")
    for f in sorted(local_fn & remote_fn):
        if local["functions"][f]["def_hash"] != remote["functions"][f]["def_hash"]:
            fn_diffs.append(f"  ~ {f}  (body differs)")
            ops.append({"type": "replace_function", "sig": f, "definition": local["functions"][f]["definition"]})

    if fn_diffs:
        lines.append("")
        lines.append("Function differences:")
        lines.extend(fn_diffs)

    # ── Triggers ──
    local_tr = set(local["triggers"])
    remote_tr = set(remote["triggers"])
    tr_diffs: list[str] = []

    for t in sorted(local_tr - remote_tr):
        tr_diffs.append(f"  + {t}  (local only)")
        ops.append({"type": "create_trigger", "key": t, "trigger": local["triggers"][t]})
    for t in sorted(remote_tr - local_tr):
        tr_diffs.append(f"  - {t}  (remote only)")
    for t in sorted(local_tr & remote_tr):
        if local["triggers"][t].get("action_statement") != remote["triggers"][t].get("action_statement"):
            tr_diffs.append(f"  ~ {t}  (action differs)")
            ops.append({"type": "replace_trigger", "key": t, "trigger": local["triggers"][t]})

    if tr_diffs:
        lines.append("")
        lines.append("Trigger differences:")
        lines.extend(tr_diffs)

    # ── Row counts ──
    rc_lines: list[str] = []
    all_tables_for_counts = sorted(common_tables)
    for t in all_tables_for_counts:
        lc = local["row_counts"].get(t, 0)
        rc = remote["row_counts"].get(t, 0)
        if lc != rc:
            rc_lines.append(f"  {t:40s}  local={lc:<8}  remote={rc}")

    if rc_lines:
        lines.append("")
        lines.append("Row count differences (common tables):")
        lines.extend(rc_lines)

    return lines, ops


# ── SQL generation ─────────────────────────────────────────────────────


async def _generate_sql(ops: list[dict], local_dsn: str) -> str:
    """Turn structured ops into an executable SQL migration script.

    The generated SQL makes the *remote* database match the *local* one
    (additive only — no DROP statements).
    """
    sections: list[str] = []

    for op in ops:
        t = op["type"]

        if t == "create_table":
            ddl = await _get_create_table_sql(local_dsn, op["table"])
            sections.append(f"-- New table: {op['table']}\n{ddl}")

        elif t == "add_column":
            col = op["col"]
            table, colname = op["key"].split(".", 1)
            typ = col["udt_name"]
            if col.get("character_maximum_length"):
                typ += f"({col['character_maximum_length']})"
            nullable = "" if col["is_nullable"] == "YES" else " NOT NULL"
            default = f" DEFAULT {col['column_default']}" if col.get("column_default") else ""
            sections.append(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {colname} {typ}{nullable}{default};"
            )

        elif t == "alter_column":
            table, colname = op["key"].split(".", 1)
            lc, rc = op["local"], op["remote"]
            stmts = []
            if lc["udt_name"] != rc["udt_name"]:
                typ = lc["udt_name"]
                if lc.get("character_maximum_length"):
                    typ += f"({lc['character_maximum_length']})"
                stmts.append(
                    f"ALTER TABLE {table} ALTER COLUMN {colname} TYPE {typ};"
                )
            if lc["is_nullable"] != rc["is_nullable"]:
                if lc["is_nullable"] == "YES":
                    stmts.append(f"ALTER TABLE {table} ALTER COLUMN {colname} DROP NOT NULL;")
                else:
                    stmts.append(f"ALTER TABLE {table} ALTER COLUMN {colname} SET NOT NULL;")
            if stmts:
                sections.append(f"-- Alter column: {op['key']}\n" + "\n".join(stmts))

        elif t == "create_index":
            sections.append(f"{op['indexdef']};")

        elif t == "replace_index":
            sections.append(f"DROP INDEX IF EXISTS {op['name']};\n{op['indexdef']};")

        elif t in ("create_function", "replace_function"):
            defn = op["definition"]
            # pg_get_functiondef() returns "CREATE OR REPLACE FUNCTION ..."
            # Ensure we don't double the CREATE OR REPLACE prefix
            if defn.upper().startswith("CREATE OR REPLACE "):
                sql_body = defn
            elif defn.upper().startswith("CREATE "):
                sql_body = "CREATE OR REPLACE " + defn[len("CREATE "):]
            else:
                sql_body = "CREATE OR REPLACE FUNCTION " + defn
            # Ensure trailing semicolon
            if not sql_body.rstrip().endswith(";"):
                sql_body = sql_body.rstrip() + ";"
            sections.append(
                f"-- {'New' if t == 'create_function' else 'Changed'} function: {op['sig']}\n"
                f"{sql_body}"
            )

        elif t == "create_trigger":
            trig = op["trigger"]
            defn = trig.get("definition")
            if defn:
                sections.append(f"-- New trigger: {op['key']}\n{defn};")
            else:
                sections.append(f"-- New trigger: {op['key']} (definition not captured)")

        elif t == "replace_trigger":
            trig = op["trigger"]
            table, name = op["key"].split(".", 1)
            defn = trig.get("definition")
            if defn:
                sections.append(
                    f"-- Changed trigger: {op['key']}\n"
                    f"DROP TRIGGER IF EXISTS {name} ON {table};\n{defn};"
                )
            else:
                sections.append(f"-- Changed trigger: {op['key']} (definition not captured)")

    return "\n\n".join(sections) + "\n" if sections else ""


# ── CLI commands ───────────────────────────────────────────────────────


def _common_remote_options():
    """Shared options for commands that need a remote connection."""
    return {
        "remote_url": typer.Option(
            None,
            "--remote-url", "-r",
            envvar="P8_REMOTE_DATABASE_URL",
            help="Remote database URL. Default: local URL with port changed to --remote-port.",
        ),
        "remote_port": typer.Option(
            _DEFAULT_REMOTE_PORT,
            "--remote-port", "-p",
            help="Port for remote database (used when --remote-url is not set).",
        ),
    }


async def _resolve_urls(remote_url: str | None, remote_port: int) -> tuple[str, str]:
    """Return (local_url, remote_url), validating connectivity."""
    from p8.settings import Settings

    settings = Settings()
    local_url = settings.database_url

    if remote_url is None:
        remote_url = _local_to_remote_url(local_url, remote_port)

    local_parts = _parse_dsn(local_url)
    remote_parts = _parse_dsn(remote_url)

    typer.echo(f"  Local:  {local_parts['host']}:{local_parts['port']}/{local_parts['database']}")
    typer.echo(f"  Remote: {remote_parts['host']}:{remote_parts['port']}/{remote_parts['database']}")
    typer.echo()

    if not _check_port_open(remote_parts["host"], remote_parts["port"]):
        raise ConnectionError(
            f"Cannot reach remote at {remote_parts['host']}:{remote_parts['port']}. "
            f"Is the port-forward running?\n\n"
            f"  kubectl port-forward svc/p8-postgres-rw {remote_parts['port']}:5432 -n p8"
        )
    if not _check_port_open(local_parts["host"], local_parts["port"]):
        raise ConnectionError(
            f"Cannot reach local at {local_parts['host']}:{local_parts['port']}. "
            f"Is the local database running?"
        )

    return local_url, remote_url


@db_app.command("diff")
def diff_command(
    remote_url: Optional[str] = typer.Option(
        None, "--remote-url", "-r", envvar="P8_REMOTE_DATABASE_URL",
        help="Remote database URL. Default: local URL with port changed to --remote-port.",
    ),
    remote_port: int = typer.Option(
        _DEFAULT_REMOTE_PORT, "--remote-port", "-p",
        help="Port for remote database (used when --remote-url is not set). "
        "Typically the local port of a kubectl port-forward.",
    ),
    tables_only: bool = typer.Option(
        False, "--tables-only",
        help="Only compare table and column structure (skip functions/triggers/indexes).",
    ),
    counts: bool = typer.Option(
        False, "--counts/--no-counts",
        help="Include row count comparison (default: no — counts are instance-specific).",
    ),
    generate: bool = typer.Option(
        False, "--generate", "-g",
        help="Generate a SQL migration file from detected differences.",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o",
        help=f"Output directory for generated migration (default: sql/migrations/).",
    ),
    message: str = typer.Option(
        "db_diff", "--message", "-m",
        help="Short label for the generated migration filename.",
    ),
):
    """Compare local database schema against a remote (port-forwarded) database.

    Typical workflow:

        kubectl port-forward svc/p8-postgres-rw 5433:5432 -n p8 &
        p8 db diff --remote-url postgresql://user:pass@localhost:5433/dbname
        p8 db diff --generate   # writes sql/migrations/NNN_db_diff.sql
        p8 db apply sql/migrations/NNN_db_diff.sql --remote-url ...
    """
    try:
        asyncio.run(_diff(
            remote_url, remote_port, tables_only, counts,
            generate, output_dir or _MIGRATIONS_DIR, message,
        ))
    except ConnectionError as exc:
        typer.secho(f"\nError: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


async def _diff(
    remote_url: str | None,
    remote_port: int,
    tables_only: bool,
    counts: bool,
    generate: bool,
    output_dir: Path,
    message: str,
):
    typer.echo("p8 db diff")
    typer.echo("=" * 60)

    local_url, remote_url = await _resolve_urls(remote_url, remote_port)

    typer.echo("Snapshotting local database...")
    local_snap = await _snapshot(local_url, "local")
    typer.echo(f"  {len(local_snap['tables'])} tables, {len(local_snap['functions'])} functions")

    typer.echo("Snapshotting remote database...")
    remote_snap = await _snapshot(remote_url, "remote")
    typer.echo(f"  {len(remote_snap['tables'])} tables, {len(remote_snap['functions'])} functions")

    # Optionally strip sections
    if tables_only:
        for snap in (local_snap, remote_snap):
            snap["indexes"] = {}
            snap["functions"] = {}
            snap["triggers"] = {}
    if not counts:
        for snap in (local_snap, remote_snap):
            snap["row_counts"] = {}

    diff_lines, ops = _diff_snapshots(local_snap, remote_snap)

    typer.echo()
    if not diff_lines:
        typer.secho("No differences detected", fg=typer.colors.GREEN, bold=True)
        return

    typer.secho(
        f"Differences detected ({len(diff_lines)} lines)",
        fg=typer.colors.YELLOW, bold=True,
    )
    for line in diff_lines:
        if line.startswith("  +"):
            typer.secho(line, fg=typer.colors.GREEN)
        elif line.startswith("  -"):
            typer.secho(line, fg=typer.colors.RED)
        elif line.startswith("  ~"):
            typer.secho(line, fg=typer.colors.YELLOW)
        else:
            typer.echo(line)

    # ── Generate migration ──
    if generate and ops:
        sql = await _generate_sql(ops, local_url)
        if not sql.strip():
            typer.echo("\nNo actionable SQL to generate (only remote-only items).")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        # Next sequence number
        existing = sorted(output_dir.glob("*.sql"))
        next_num = 1
        for f in existing:
            try:
                next_num = max(next_num, int(f.stem.split("_")[0]) + 1)
            except (ValueError, IndexError):
                pass

        safe_msg = message.replace(" ", "_").replace("-", "_")[:40]
        filename = f"{next_num:03d}_{safe_msg}.sql"
        outpath = output_dir / filename

        header = (
            f"-- Migration: {message}\n"
            f"-- Generated by: p8 db diff --generate\n"
            f"-- Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"-- Direction: local → remote (additive)\n"
            f"--\n"
            f"-- Review before applying!\n"
            f"--   p8 db apply {outpath.relative_to(Path.cwd()) if outpath.is_relative_to(Path.cwd()) else outpath}"
            f" --remote-url <REMOTE_URL>\n"
            f"--\n\n"
        )

        outpath.write_text(header + sql)
        typer.echo()
        typer.secho(f"Migration generated: {outpath}", fg=typer.colors.GREEN, bold=True)
        typer.echo(f"  {len(ops)} operation(s)")
        typer.echo()
        typer.echo("Next steps:")
        typer.echo(f"  1. Review: cat {outpath}")
        typer.echo(f"  2. Apply:  p8 db apply {outpath} --remote-url <REMOTE_URL>")


@db_app.command("apply")
def apply_command(
    sql_file: Path = typer.Argument(..., help="SQL migration file to apply.", exists=True),
    target_url: Optional[str] = typer.Option(
        None, "--remote-url", "-r", envvar="P8_REMOTE_DATABASE_URL",
        help="Target database URL. If omitted, applies to local database.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print SQL without executing.",
    ),
):
    """Apply a SQL migration file to a database.

    By default applies to the local database. Use --remote-url to target
    the remote (port-forwarded) database instead.

    Examples:

        p8 db apply sql/migrations/001_db_diff.sql
        p8 db apply sql/migrations/001_db_diff.sql --remote-url postgresql://...
        p8 db apply sql/migrations/001_db_diff.sql --dry-run
    """
    try:
        asyncio.run(_apply(sql_file, target_url, dry_run))
    except ConnectionError as exc:
        typer.secho(f"\nError: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


async def _apply(sql_file: Path, target_url: str | None, dry_run: bool):
    import asyncpg
    import re

    sql = sql_file.read_text()

    if dry_run:
        typer.echo(f"-- Dry run: {sql_file.name}")
        typer.echo("-" * 60)
        typer.echo(sql)
        typer.echo("-" * 60)
        typer.secho("No changes made (dry run)", fg=typer.colors.YELLOW)
        return

    # Resolve target
    if target_url is None:
        from p8.settings import Settings
        target_url = Settings().database_url

    parts = _parse_dsn(target_url)
    typer.echo(f"Applying {sql_file.name} to {parts['host']}:{parts['port']}/{parts['database']}")

    if not _check_port_open(parts["host"], parts["port"]):
        raise ConnectionError(
            f"Cannot reach {parts['host']}:{parts['port']}. Is the database / port-forward running?"
        )

    try:
        conn = await asyncpg.connect(target_url)
    except Exception as exc:
        raise ConnectionError(f"Cannot connect: {exc}") from exc

    # Split on statement boundaries, respecting dollar-quoted blocks.
    # asyncpg's execute() uses the extended protocol which only handles
    # single statements; we must split and run each one in a transaction.
    statements = _split_sql(sql)

    try:
        async with conn.transaction():
            for i, stmt in enumerate(statements, 1):
                await conn.execute(stmt)
        typer.secho(f"Applied successfully ({len(statements)} statement(s))", fg=typer.colors.GREEN, bold=True)
    except Exception as exc:
        typer.secho(f"Failed on statement {i}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    finally:
        await conn.close()


def _split_sql(sql: str) -> list[str]:
    """Split a SQL script into individual statements, respecting dollar-quoting.

    Naive semicolon splitting breaks on $function$...$function$ blocks.
    This parser tracks dollar-quote depth to split correctly.
    """
    statements: list[str] = []
    current: list[str] = []
    in_dollar_quote: str | None = None  # e.g. "$function$"

    for line in sql.split("\n"):
        stripped = line.strip()

        # Skip pure comment / blank lines between statements
        if not current and (not stripped or stripped.startswith("--")):
            continue

        # Track dollar-quote open/close
        if in_dollar_quote is None:
            # Check if a dollar-quote opens on this line
            match = re.search(r'(\$[a-zA-Z_]*\$)', line)
            if match:
                tag = match.group(1)
                # Check if it also closes on the same line (unlikely for functions)
                count = line.count(tag)
                if count % 2 == 1:
                    in_dollar_quote = tag
        else:
            # Inside a dollar-quoted block — check if it closes
            if in_dollar_quote in line:
                count = line.count(in_dollar_quote)
                if count % 2 == 1:
                    in_dollar_quote = None

        current.append(line)

        # Statement ends at a semicolon outside dollar-quoting
        if in_dollar_quote is None and stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt and not all(l.strip().startswith("--") or not l.strip() for l in current):
                statements.append(stmt)
            current = []

    # Leftover (shouldn't happen with well-formed SQL)
    if current:
        stmt = "\n".join(current).strip()
        if stmt and not all(l.strip().startswith("--") or not l.strip() for l in current):
            statements.append(stmt)

    return statements
