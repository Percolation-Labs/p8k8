"""p8 migrate — run database bootstrap scripts.

Executes install_entities.sql (entity tables, mutable) then install.sql
(core infrastructure, stable) against the configured database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

import p8.services.bootstrap as _svc

migrate_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)

_SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "sql"


async def _run_migrate():
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        scripts = [
            _SQL_DIR / "install_entities.sql",
            _SQL_DIR / "install.sql",
            _SQL_DIR / "payments.sql",
        ]

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
def migrate_command():
    """Run database migration scripts (install_entities.sql + install.sql)."""
    asyncio.run(_run_migrate())
