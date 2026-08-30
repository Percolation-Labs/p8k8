"""p8 percolate — run the detection pipeline and browse its output.

``p8 percolate ingest`` is the manual verification path: a real run against
live sources producing actual scored, linked event nodes (no mocks).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

import p8.services.bootstrap as _svc

percolate_app = typer.Typer(no_args_is_help=True)


async def _ingest(source: str | None, limit: int):
    from p8.services.percolate.pipeline import run_ingestion_cycle

    async with _svc.bootstrap_services(include_embeddings=True) as (
        db, encryption, settings, _file_service, _content_service, embedding_service, _queue,
    ):
        source_keys = [source] if source else None
        stats = await run_ingestion_cycle(
            db=db, encryption=encryption, embedding_service=embedding_service,
            settings=settings, source_keys=source_keys, limit=limit,
        )

        typer.echo(f"\nItems ingested: {stats['items_ingested']}")
        typer.echo(f"Events created: {stats['events_created']}  updated: {stats['events_updated']}  "
                    f"interpreted: {stats['events_interpreted']}")
        for key, s in stats["sources"].items():
            typer.echo(f"  {key:<28} items={s['items']:<4} created={s['events_created']:<4} "
                       f"updated={s['events_updated']:<4} interpreted={s['events_interpreted']}")
        if stats["errors"]:
            typer.echo("\nErrors:")
            for e in stats["errors"]:
                typer.echo(f"  - {e}")


@percolate_app.command()
def ingest(
    source: Optional[str] = typer.Option(
        None, "--source", "-s",
        help="Single source key (sec_edgar, arxiv, hn, federal_register, wikipedia_recent_changes). Omit to run all enabled sources.",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Max items to fetch per source"),
):
    """Run one real ingestion cycle against live sources — the manual proof
    that the detection pipeline works end to end on real data."""
    asyncio.run(_ingest(source, limit))


async def _feed(status: str, limit: int):
    async with _svc.bootstrap_services() as (db, _encryption, _settings, *_rest):
        rows = await db.fetch(
            "SELECT name, title, category, classification, corroboration_score, "
            "primary_source_url, event_time FROM percolate_events "
            "WHERE status = $1 AND deleted_at IS NULL "
            "ORDER BY corroboration_score DESC, event_time DESC NULLS LAST LIMIT $2",
            status, limit,
        )
        if not rows:
            typer.echo(f"No {status} events found. Run `p8 percolate ingest` first.")
            return

        for r in rows:
            typer.echo(f"\n[{r['category'] or '?':<10}] {r['title']}")
            if r["classification"]:
                typer.echo(f"  classification={r['classification']}  corroboration={r['corroboration_score']:.2f}")
            typer.echo(f"  source: {r['primary_source_url']}")


@percolate_app.command()
def feed(
    status: str = typer.Option("interpreted", "--status", help="raw | thresholded | interpreted"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Print the current feed of detected events."""
    asyncio.run(_feed(status, limit))
