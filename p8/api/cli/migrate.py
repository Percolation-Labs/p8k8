"""p8 migrate — run database bootstrap scripts.

Runs all sql/*.sql files in lexicographic order (01_, 02_, …).
Optionally pass specific filenames to run a subset.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

import p8.services.bootstrap as _svc

migrate_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "sql"


async def _run_migrate(files: list[str] | None = None):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        if files:
            scripts = [_SQL_DIR / f for f in files]
        else:
            scripts = sorted(_SQL_DIR.glob("*.sql"))

        if not scripts:
            typer.echo("No SQL scripts found in sql/", err=True)
            raise typer.Exit(1)

        for script in scripts:
            if not script.exists():
                typer.echo(f"Warning: {script} not found, skipping", err=True)
                continue
            sql = script.read_text()
            typer.echo(f"Running {script.name}...")
            await db.execute(sql)
            typer.echo(f"  {script.name} applied")

        typer.echo("Migration complete")


@migrate_app.callback()
def migrate_command(
    files: Optional[list[str]] = typer.Argument(None, help="Specific SQL files to run (default: all sql/*.sql in order)"),
):
    """Run database migration scripts (all sql/*.sql in sorted order)."""
    asyncio.run(_run_migrate(files))
