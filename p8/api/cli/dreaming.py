"""p8 dream — run dreaming for a user from the CLI."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional
from uuid import UUID

import typer
import yaml

import p8.services.bootstrap as _svc
from p8.utils.parsing import ensure_parsed


def _clean(v):
    """Normalize DB values for YAML output."""
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return v


async def _run_dream(
    user_id: UUID,
    lookback_days: int,
    allow_empty: bool,
    output_path: str | None,
):
    from p8.api.tools import init_tools, set_tool_context
    from p8.workers.handlers.dreaming import DreamingHandler

    async with _svc.bootstrap_services() as (db, encryption, _settings, *_rest):
        init_tools(db, encryption)
        set_tool_context(user_id=user_id)

        class Ctx:
            db: object
            encryption: object
        ctx = Ctx()
        ctx.db = db  # type: ignore[assignment]
        ctx.encryption = encryption  # type: ignore[assignment]

        handler = DreamingHandler()
        task = {
            "user_id": str(user_id),
            "lookback_days": lookback_days,
            "allow_empty_activity_dreaming": allow_empty,
        }
        result = await handler.handle(task, ctx)

        phase1 = result.get("phase1", {})
        phase2 = result.get("phase2", {})

        typer.echo(f"\nPhase 1: {phase1.get('moments_built', 0)} session chunks built "
                   f"({phase1.get('sessions_checked', 0)} sessions checked)")
        typer.echo(f"Phase 2: {phase2.get('moments_saved', 0)} dream moments saved "
                   f"({phase2.get('io_tokens', 0)} API tokens)")

        if phase2.get("status") == "error":
            typer.echo(f"  Error: {phase2.get('error')}", err=True)
            raise typer.Exit(1)

        # Query dream moments just created
        dreams = await db.fetch(
            "SELECT name, moment_type, summary, topic_tags, graph_edges, metadata, "
            "       source_session_id, created_at "
            "FROM moments WHERE user_id = $1 AND moment_type = 'dream' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT $2",
            user_id, phase2.get("moments_saved", 3),
        )

        # Query back-edges on source tables
        back_edge_rows = await db.fetch(
            "SELECT 'moments' AS source_table, name, moment_type AS subtype, graph_edges "
            "FROM moments WHERE user_id = $1 AND deleted_at IS NULL "
            "AND graph_edges::text LIKE '%%dreamed_from%%' "
            "UNION ALL "
            "SELECT 'resources', name, category, graph_edges "
            "FROM resources WHERE user_id = $1 AND deleted_at IS NULL "
            "AND graph_edges::text LIKE '%%dreamed_from%%'",
            user_id,
        )

        # Print summary to terminal
        for d in dreams:
            edges = ensure_parsed(d["graph_edges"], default=[])
            tags = ensure_parsed(d["topic_tags"], default=[])
            typer.echo(f"\n  {d['name']}")
            typer.echo(f"    {d['summary'][:120]}...")
            typer.echo(f"    tags: {', '.join(tags) if tags else ''}")
            for e in (edges or []):
                typer.echo(f"    -> {e.get('target')} [{e.get('relation')}] w={e.get('weight')}")

        # Write YAML output if requested
        if output_path:
            back_edges_out: list[dict] = []
            out = {
                "run": {
                    "user_id": str(user_id),
                    "lookback_days": lookback_days,
                    "phase1": {
                        "session_chunks_built": phase1.get("moments_built", 0),
                        "sessions_checked": phase1.get("sessions_checked", 0),
                    },
                    "phase2": {
                        "dream_moments_saved": phase2.get("moments_saved", 0),
                        "io_tokens": phase2.get("io_tokens", 0),
                        "session_id": phase2.get("session_id"),
                    },
                },
                "dream_moments": [
                    {k: _clean(d[k]) for k in d.keys()} for d in dreams
                ],
                "back_edges": back_edges_out,
            }
            for r in back_edge_rows:
                edges = ensure_parsed(r["graph_edges"], default=[])
                dreamed = [e for e in (edges or []) if e.get("relation") == "dreamed_from"]
                if dreamed:
                    back_edges_out.append({
                        "source_table": r["source_table"],
                        "name": r["name"],
                        "subtype": r["subtype"],
                        "dreamed_from_edges": dreamed,
                    })

            with open(output_path, "w") as f:
                yaml.dump(out, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
            typer.echo(f"\nWrote {output_path}")


def register_dream_command(app: typer.Typer):
    """Register the dream command directly on the main app."""

    @app.command()
    def dream(
        user_id: str = typer.Argument(..., help="User UUID to run dreaming for"),
        lookback_days: int = typer.Option(1, "--lookback", "-l", help="Date window in days"),
        allow_empty: bool = typer.Option(False, "--allow-empty", "-e", help="Exploration mode even with no activity"),
        output: Optional[str] = typer.Option(None, "--output", "-o", help="Write full results to YAML file"),
    ):
        """Run dreaming for a user — consolidation + AI insights."""
        asyncio.run(_run_dream(UUID(user_id), lookback_days, allow_empty, output))
