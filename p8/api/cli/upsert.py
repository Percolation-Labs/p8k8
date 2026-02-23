"""p8 upsert — bulk upsert from JSON/YAML/Markdown files.

Usage patterns:
  p8 upsert schemas data/agents.yaml          # JSON/YAML → explicit table
  p8 upsert resources data/chunks.json         # JSON/YAML → explicit table
  p8 upsert servers data/servers.yaml          # JSON/YAML → explicit table
  p8 upsert docs/architecture.md               # Markdown → ontologies (default)
  p8 upsert docs/                              # Folder of .md → ontologies (default)
  p8 upsert resources docs/architecture.pdf    # Any file → File + Resource chunks

Convention:
  - Markdown files → ontologies by default (one Ontology per file, name=stem, content=body).
  - Resources via ContentService: file → extract → chunk → File + Resources.
  - JSON/YAML files require an explicit table name.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from uuid import UUID

import typer

import p8.services.bootstrap as _svc
from p8.ontology.base import CoreModel
from p8.ontology.types import TABLE_MAP
from p8.services.content import load_structured

upsert_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


def _resolve_model(table: str) -> type[CoreModel]:
    if table not in TABLE_MAP:
        valid = ", ".join(sorted(TABLE_MAP))
        raise typer.BadParameter(f"Unknown table '{table}'. Valid tables: {valid}")
    return TABLE_MAP[table]


async def _run_upsert(
    table: str | None,
    path: str,
    tenant_id: str | None,
    user_id: UUID | None,
):
    async with _svc.bootstrap_services() as (db, encryption, settings, file_service, content_service, *_rest):
        p = Path(path)
        is_dir = p.is_dir()
        is_markdown = not is_dir and p.suffix.lower() == ".md"
        is_structured = not is_dir and p.suffix.lower() in (".json", ".yaml", ".yml")

        # --- Resources: any file/dir → ContentService (extract + chunk) ---
        if table == "resources" and not is_structured:
            _resolve_model("resources")  # validate table name
            if is_dir:
                results = await content_service.ingest_directory(
                    path, tenant_id=tenant_id, user_id=user_id
                )
                total_chunks = sum(r.chunk_count for r in results)
                for r in results:
                    typer.echo(
                        f"  {r.file.name}: {r.chunk_count} chunks, "
                        f"file_id={r.file.id}"
                    )
                typer.echo(
                    f"Ingested {len(results)} file(s), "
                    f"{total_chunks} total chunks into resources"
                )
            else:
                result = await content_service.ingest_path(
                    path, tenant_id=tenant_id, user_id=user_id
                )
                typer.echo(
                    f"  {result.file.name}: {result.chunk_count} chunks, "
                    f"file_id={result.file.id}"
                )
                typer.echo(
                    f"Ingested 1 file, {result.chunk_count} chunks into resources"
                )
            return

        # --- Markdown files/folders → ontologies (or other table) ---
        if is_dir or is_markdown:
            target_table = table or "ontologies"
            model_class = _resolve_model(target_table)

            if is_dir:
                files = file_service.list_dir(path, "**/*.md")
                if not files:
                    typer.echo(f"No .md files found in {path}", err=True)
                    raise typer.Exit(1)
            else:
                files = [path]

            result = await content_service.upsert_markdown(
                files, model_class=model_class, tenant_id=tenant_id, user_id=user_id
            )
            typer.echo(f"Upserted {result.count} rows into {result.table}")
            return

        # --- JSON/YAML → explicit table required ---
        if is_structured:
            if not table:
                typer.echo(
                    "Error: JSON/YAML files require a table name.\n"
                    "  Usage: p8 upsert <table> <path>",
                    err=True,
                )
                raise typer.Exit(1)

            model_class = _resolve_model(table)
            text = await file_service.read_text(path)
            items = load_structured(text, path)

            result = await content_service.upsert_structured(
                items, model_class, tenant_id=tenant_id, user_id=user_id
            )
            typer.echo(f"Upserted {result.count} rows into {result.table}")
            return

        # --- Unsupported ---
        typer.echo(
            f"Error: unsupported path '{path}'. "
            "Expected .json, .yaml, .yml, .md file or directory.",
            err=True,
        )
        raise typer.Exit(1)


@upsert_app.callback()
def upsert_command(
    args: list[str] = typer.Argument(
        help="[table] <path> — table is required for JSON/YAML, defaults to ontologies for .md",
    ),
    tenant_id: Optional[str] = typer.Option(
        None, "--tenant-id", "-t",
        help="Stamp tenant_id on rows. Omit unless you need tenant-scoped encryption/isolation.",
    ),
    user_id: Optional[str] = typer.Option(
        None, "--user-id", "-u",
        help="Override user_id on all rows (recomputes deterministic IDs for target user). "
             "Omit to keep whatever user_id is in the data.",
    ),
):
    """Bulk upsert entities from JSON/YAML/Markdown files.

    Markdown files default to 'ontologies'. Resources use ContentService for
    extraction + chunking. JSON/YAML require an explicit table name.

    Examples:
      p8 upsert schemas data/agents.yaml
      p8 upsert docs/architecture.md
      p8 upsert docs/
      p8 upsert resources paper.pdf
    """
    if len(args) == 2:
        table, path = args
    elif len(args) == 1:
        arg = args[0]
        if Path(arg).suffix or Path(arg).is_dir():
            table, path = None, arg
        else:
            typer.echo(
                f"Error: '{arg}' doesn't look like a file path. "
                "Usage: p8 upsert [table] <path>",
                err=True,
            )
            raise typer.Exit(1)
    else:
        typer.echo("Usage: p8 upsert [table] <path>", err=True)
        raise typer.Exit(1)

    uid = UUID(user_id) if user_id else None
    asyncio.run(_run_upsert(table, path, tenant_id, uid))
