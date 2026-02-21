"""p8 query — execute REM dialect queries (one-shot or REPL)."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional
from uuid import UUID

import typer

import p8.services.bootstrap as _svc

query_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


def _print_results(results: list[dict], fmt: str = "json"):
    """Print query results in the requested format."""
    if fmt == "table":
        if not results:
            typer.echo("(no results)")
            return
        # Simple tabular output: header row + data rows
        # Flatten each result to a single level for display
        rows = []
        for r in results:
            if isinstance(r, dict) and "data" in r:
                flat = {"entity_type": r.get("entity_type", "")}
                data = r["data"]
                if isinstance(data, dict):
                    flat.update(data)
                else:
                    flat["data"] = data
                rows.append(flat)
            else:
                rows.append(r if isinstance(r, dict) else {"value": r})

        if not rows:
            typer.echo("(no results)")
            return

        keys = list(dict.fromkeys(k for row in rows for k in row))
        # Truncate wide columns
        max_col = 40
        header = " | ".join(k[:max_col].ljust(max_col) for k in keys)
        typer.echo(header)
        typer.echo("-" * len(header))
        for row in rows:
            vals = []
            for k in keys:
                v = str(row.get(k, ""))
                if len(v) > max_col:
                    v = v[: max_col - 3] + "..."
                vals.append(v.ljust(max_col))
            typer.echo(" | ".join(vals))
    else:
        typer.echo(json.dumps(results, indent=2, default=str))


async def _run_query(
    query: str | None,
    tenant_id: str | None,
    user_id: UUID | None,
    fmt: str,
):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        if query:
            # One-shot mode
            try:
                results = await db.rem_query(query, tenant_id=tenant_id, user_id=user_id)
                _print_results(results, fmt)
            except (ValueError, Exception) as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(1)
        else:
            # REPL mode
            typer.echo("REM query REPL (Ctrl+C to exit)")
            while True:
                try:
                    line = input("rem> ").strip()
                except (KeyboardInterrupt, EOFError):
                    typer.echo("\nBye.")
                    break
                if not line:
                    continue
                if line.lower() in ("exit", "quit", "\\q"):
                    break
                try:
                    results = await db.rem_query(line, tenant_id=tenant_id, user_id=user_id)
                    _print_results(results, fmt)
                except (ValueError, Exception) as e:
                    typer.echo(f"Error: {e}", err=True)


@query_app.callback()
def query_command(
    query: Optional[str] = typer.Argument(None, help="REM dialect query string"),
    tenant_id: Optional[str] = typer.Option(
        None, "--tenant-id", "-t",
        help="Filter by tenant. Omit to query public data only.",
    ),
    user_id: Optional[str] = typer.Option(
        None, "--user-id", "-u",
        help="Filter by user. Omit to query without user scope.",
    ),
    fmt: str = typer.Option("json", "--format", "-f", help="Output format: json | table"),
):
    """Execute a REM query. If no query given, starts interactive REPL."""
    uid = UUID(user_id) if user_id else None
    asyncio.run(_run_query(query, tenant_id, uid, fmt))
