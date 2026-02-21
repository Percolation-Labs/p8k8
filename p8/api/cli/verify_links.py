"""p8 verify-links — check markdown link targets in ontology files.

Usage:
  p8 verify-links docs/ontology/
  p8 verify-links docs/ontology/ --db    # also check KV store
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

from p8.utils.links import verify_links

verify_links_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


async def _run_verify_links(path: str, check_db: bool):
    from p8.utils.links import verify_links_with_db

    if check_db:
        import p8.services.bootstrap as _svc

        async with _svc.bootstrap_services() as (db, *_rest):
            report = await verify_links_with_db(path, db)
    else:
        report = verify_links(path)

    typer.echo(
        f"Links: {report.total_links} total, "
        f"{report.resolved} resolved, "
        f"{report.skipped} skipped (URLs/anchors), "
        f"{report.broken} broken"
    )

    if report.issues:
        typer.echo()
        for issue in report.issues:
            typer.echo(f"  {issue.file}:{issue.line}  [{issue.text}]({issue.target})")
            typer.echo(f"    {issue.message}")
        raise typer.Exit(1)


@verify_links_app.callback()
def verify_links_command(
    path: str = typer.Argument(help="Path to ontology directory to scan"),
    check_db: bool = typer.Option(
        False, "--db",
        help="Also check unresolved targets against the KV store (requires running DB)",
    ),
):
    """Verify markdown links in ontology files.

    Scans all .md files for [text](target) links and checks that each target
    resolves to another ontology file's stem name. With --db, also checks the
    KV store for entity keys.

    Examples:
      p8 verify-links docs/ontology/
      p8 verify-links docs/ontology/ --db
    """
    asyncio.run(_run_verify_links(path, check_db))
