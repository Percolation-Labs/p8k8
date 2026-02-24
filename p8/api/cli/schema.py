"""p8 schema — list, get, and delete schemas."""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

import p8.services.bootstrap as _svc
from p8.ontology.types import Schema
from p8.ontology.verify import register_models, verify_all
from p8.services.repository import Repository

schema_app = typer.Typer(no_args_is_help=True)
_con = Console()


async def _list_schemas(kind: str | None, limit: int):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        repo = Repository(Schema, db, encryption)
        filters = {"kind": kind} if kind else None
        entities = await repo.find(filters=filters, limit=limit)

        table = Table(title=f"Schemas ({len(entities)})", show_lines=False)
        table.add_column("Kind", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Description", style="dim")

        for e in entities:
            desc = (e.description or "")[:80]
            preview = desc + "..." if len(e.description or "") > 80 else desc
            table.add_row(e.kind, e.name, preview)

        _con.print(table)


async def _get_schema(schema_id: str):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        repo = Repository(Schema, db, encryption)
        result = await repo.get(UUID(schema_id))
        if not result:
            typer.echo(f"Schema {schema_id} not found", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


async def _delete_schema(schema_id: str):
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        repo = Repository(Schema, db, encryption)
        deleted = await repo.delete(UUID(schema_id))
        if not deleted:
            typer.echo(f"Schema {schema_id} not found", err=True)
            raise typer.Exit(1)
        typer.echo(f"Deleted {schema_id}")


@schema_app.command("list")
def list_command(
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help="Filter by kind (agent, model, etc.)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
):
    """List registered schemas."""
    asyncio.run(_list_schemas(kind, limit))


@schema_app.command("get")
def get_command(
    schema_id: str = typer.Argument(help="Schema UUID"),
):
    """Get a schema by ID (full JSON output)."""
    asyncio.run(_get_schema(schema_id))


@schema_app.command("delete")
def delete_command(
    schema_id: str = typer.Argument(help="Schema UUID to soft-delete"),
):
    """Soft-delete a schema."""
    asyncio.run(_delete_schema(schema_id))


# ---------------------------------------------------------------------------
# verify / register — DDL verification and model registration
# ---------------------------------------------------------------------------


async def _verify():
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        issues = await verify_all(db)

    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    for issue in issues:
        marker = "ERROR" if issue.level == "error" else "WARN "
        typer.echo(f"  [{marker}] {issue.table:20s} {issue.check:30s} {issue.message}")

    typer.echo(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        raise typer.Exit(1)


async def _register():
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        count = await register_models(db)

    typer.echo(f"Registered {count} model(s) into schemas (kind='table')")


@schema_app.command("verify")
def verify_command():
    """Verify that live DB matches pydantic model declarations."""
    asyncio.run(_verify())


@schema_app.command("register")
def register_command():
    """Sync model metadata into schemas table from Python source of truth."""
    asyncio.run(_register())
