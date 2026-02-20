"""p8 moments — list moments and view session timelines."""

from __future__ import annotations

import asyncio
from typing import Optional
from uuid import UUID

import typer

import services.bootstrap as _svc
from p8.services.memory import MemoryService

moments_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


async def _list_moments(
    moment_type: str | None,
    user_id: UUID | None,
    limit: int,
):
    from p8.ontology.types import Moment
    from p8.services.repository import Repository

    async with _svc.bootstrap_services() as (db, encryption, _settings, *_rest):
        # Show today summary first
        memory = MemoryService(db, encryption)
        today = await memory.build_today_summary(user_id=user_id)
        if today:
            typer.echo(f"\n  {today['summary']}\n")

        # List recent moments
        repo = Repository(Moment, db, encryption)
        filters = {}
        if moment_type:
            filters["moment_type"] = moment_type
        moments = await repo.find(user_id=user_id, filters=filters, limit=limit)

        if not moments:
            typer.echo("No moments found.")
            return

        typer.echo(f"{'TYPE':<20} {'NAME':<40} {'CREATED':<20}")
        typer.echo("-" * 80)
        for m in moments:
            ts = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""
            typer.echo(f"{m.moment_type or '':<20} {m.name:<40} {ts:<20}")


@moments_app.callback()
def list_moments(
    ctx: typer.Context,
    moment_type: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by moment_type"),
    user_id: Optional[str] = typer.Option(None, "--user-id", "-u", help="Filter by user_id"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results"),
):
    """List moments. Shows today's summary first, then recent moments."""
    if ctx.invoked_subcommand is not None:
        return
    uid = UUID(user_id) if user_id else None
    asyncio.run(_list_moments(moment_type, uid, limit))


async def _timeline(session_id: str, limit: int):
    async with _svc.bootstrap_services() as (db, _encryption, _settings, *_rest):
        rows = await db.rem_session_timeline(UUID(session_id), limit=limit)

        if not rows:
            typer.echo("No events found for this session.")
            return

        typer.echo(f"\n{'TYPE':<8} {'TIMESTAMP':<22} {'KIND':<18} {'CONTENT':<50}")
        typer.echo("-" * 98)
        for r in rows:
            tag = "[MSG]" if r["event_type"] == "message" else "[MOM]"
            ts = r["event_timestamp"].strftime("%Y-%m-%d %H:%M:%S") if r["event_timestamp"] else ""
            kind = r["name_or_type"] or ""
            content = (r["content_or_summary"] or "")[:50]
            typer.echo(f"{tag:<8} {ts:<22} {kind:<18} {content}")


@moments_app.command()
def timeline(
    session_id: str = typer.Argument(..., help="Session UUID"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max events"),
):
    """Show interleaved messages + moments for a session."""
    asyncio.run(_timeline(session_id, limit))


async def _compact(session_id: str, threshold: int):
    async with _svc.bootstrap_services() as (db, encryption, _settings, *_rest):
        memory = MemoryService(db, encryption)
        moment = await memory.maybe_build_moment(
            UUID(session_id), threshold=threshold,
        )
        if moment:
            typer.echo(f"Created moment: {moment.name}")
        else:
            typer.echo("No compaction needed (tokens since last moment below threshold).")


@moments_app.command()
def compact(
    session_id: str = typer.Argument(..., help="Session UUID to compact"),
    threshold: int = typer.Option(200, "--threshold", "-t", help="Token threshold"),
):
    """Trigger moment compaction for a session."""
    asyncio.run(_compact(session_id, threshold))
