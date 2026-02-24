"""p8 dream — run dreaming for a user from the CLI."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional
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


async def _simulate_dream():
    """Seed fixture data, run full dreaming pipeline, and inspect results."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.services.usage import check_quota
    from p8.workers.handlers.dreaming import DreamingHandler
    from tests.integration.dreaming.fixtures import TEST_USER_ID, setup_dreaming_fixtures

    log = logging.getLogger("simulate")
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s  %(message)s")
    for name in ("httpx", "httpcore", "openai", "pydantic_ai"):
        logging.getLogger(name).setLevel(logging.WARNING)

    async with _svc.bootstrap_services() as (db, encryption, _settings, *_rest):
        # 1. Clean prior state
        log.info("=== CLEANUP ===")
        await db.execute(
            "DELETE FROM moments WHERE moment_type IN ('dream', 'session_chunk') AND user_id = $1",
            TEST_USER_ID,
        )
        await db.execute(
            "DELETE FROM messages WHERE session_id IN "
            "(SELECT id FROM sessions WHERE mode = 'dreaming' AND user_id = $1)", TEST_USER_ID,
        )
        await db.execute("DELETE FROM sessions WHERE mode = 'dreaming' AND user_id = $1", TEST_USER_ID)
        await db.execute(
            "DELETE FROM usage_tracking WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens'",
            TEST_USER_ID,
        )
        log.info("Cleaned prior dreaming state for user %s", TEST_USER_ID)

        # 2. Seed fixtures
        log.info("\n=== SEED FIXTURES ===")
        sa, sb, ma, mb, ra, rb = await setup_dreaming_fixtures(db, encryption)
        log.info("Created sessions: %s, %s", sa.name, sb.name)
        log.info("Created moments:  %s, %s (+ content_upload moments for each)", ma.name, mb.name)
        log.info("Created resources: %s, %s", ra.name, rb.name)

        # 3. Pre-flight state
        log.info("\n=== PRE-FLIGHT STATE ===")
        status = await check_quota(db, TEST_USER_ID, "dreaming_io_tokens", "free")
        log.info("dreaming_io_tokens quota: used=%d, limit=%d, exceeded=%s", status.used, status.limit, status.exceeded)

        # 4. Run DreamingHandler
        log.info("\n=== RUNNING DREAMING HANDLER ===")
        init_tools(db, encryption)
        set_tool_context(user_id=TEST_USER_ID)

        class Ctx:
            db: Any = None
            encryption: Any = None
        ctx = Ctx()
        ctx.db = db
        ctx.encryption = encryption

        handler = DreamingHandler()
        text, stats = await handler._load_dreaming_context(TEST_USER_ID, lookback_days=1, db=db, encryption=encryption)
        log.info("Context loaded: %d chars", len(text))
        log.info("Context stats: %s", json.dumps(stats, indent=2))
        log.info("Context preview (first 500 chars):\n%s", text[:500])

        result = await handler.handle({"user_id": str(TEST_USER_ID), "lookback_days": 1}, ctx)
        log.info("\n=== HANDLER RESULT ===")
        log.info("Total io_tokens: %d", result.get("io_tokens", 0))
        log.info("Phase 1: %s", json.dumps(result.get("phase1", {}), indent=2))
        log.info("Phase 2: %s", json.dumps(result.get("phase2", {}), indent=2, default=str))

        # 5. Phase 1 — session_chunk moments with resource enrichment
        log.info("\n=== PHASE 1 — SESSION CHUNKS ===")
        phase1 = result.get("phase1", {})
        log.info("Sessions checked: %d, moments built: %d",
                 phase1.get("sessions_checked", 0), phase1.get("moments_built", 0))
        chunks = await db.fetch(
            "SELECT name, summary, metadata FROM moments "
            "WHERE user_id = $1 AND moment_type = 'session_chunk' "
            "AND deleted_at IS NULL ORDER BY created_at DESC",
            TEST_USER_ID,
        )
        for c in chunks:
            meta = ensure_parsed(c["metadata"], default={}) or {}
            resource_keys = meta.get("resource_keys", []) if isinstance(meta, dict) else []
            has_resources = "[Uploaded Resources]" in (c["summary"] or "")
            log.info("  %s (resource_keys=%s, enriched=%s)", c["name"], resource_keys, has_resources)
            if has_resources:
                # Show the resource section
                idx = c["summary"].index("[Uploaded Resources]")
                log.info("    %s", c["summary"][idx:idx+200])

        # 6. Inspect Phase 2 dreaming session
        log.info("\n=== DREAMING SESSION ===")
        phase2 = result.get("phase2", {})
        session_id = phase2.get("session_id")
        if session_id:
            session_id = UUID(session_id)
            rows = await db.fetch(
                "SELECT id, name, mode, agent_name, total_tokens, created_at "
                "FROM sessions WHERE id = $1", session_id,
            )
            for r in rows:
                log.info("  id=%s name=%s mode=%s agent=%s tokens=%s",
                         r["id"], r["name"], r["mode"], r["agent_name"], r["total_tokens"])

            log.info("\n=== SESSION MESSAGES ===")
            msgs = await db.fetch(
                "SELECT message_type, content, tool_calls, token_count "
                "FROM messages WHERE session_id = $1 AND deleted_at IS NULL "
                "ORDER BY created_at", session_id,
            )
            for i, m in enumerate(msgs):
                mtype = m["message_type"]
                content = (m["content"] or "")[:200]
                tc = m["tool_calls"]
                if mtype == "assistant" and tc:
                    calls = tc.get("calls", []) if isinstance(tc, dict) else []
                    names = [c["name"] for c in calls]
                    log.info("  [%d] %s — tool_calls=%s | %s", i, mtype, names, content[:100])
                elif mtype == "tool_call" and tc:
                    name = tc.get("name", "") if isinstance(tc, dict) else ""
                    log.info("  [%d] %s (%s) — %s", i, mtype, name, content[:150])
                else:
                    log.info("  [%d] %s — %s", i, mtype, content[:150])
            log.info("  Total messages: %d", len(msgs))

        # 7. Dream moments
        log.info("\n=== DREAM MOMENTS ===")
        dreams = await db.fetch(
            "SELECT name, summary, topic_tags, emotion_tags, graph_edges, metadata, moment_type "
            "FROM moments WHERE user_id = $1 AND moment_type = 'dream' "
            "ORDER BY created_at DESC", TEST_USER_ID,
        )
        log.info("Dream moments found: %d", len(dreams))
        for d in dreams:
            log.info("\n  --- %s ---", d["name"])
            log.info("  summary: %s", d["summary"])
            tags = ensure_parsed(d["topic_tags"], default=[])
            log.info("  topic_tags: %s", tags)
            emotions = ensure_parsed(d["emotion_tags"], default=[])
            log.info("  emotion_tags: %s", emotions)
            edges = ensure_parsed(d["graph_edges"], default=[])
            log.info("  graph_edges (%d):", len(edges) if edges else 0)
            for e in (edges or []):
                log.info("    -> %s [%s] weight=%.1f reason=%s",
                         e.get("target"), e.get("relation"), e.get("weight", 0), e.get("reason", ""))

        # 8. Usage tracking
        log.info("\n=== USAGE TRACKING ===")
        usage_row = await db.fetchrow(
            "SELECT used, period_start FROM usage_tracking "
            "WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens' "
            "AND period_start = date_trunc('month', CURRENT_DATE)::date",
            TEST_USER_ID,
        )
        if usage_row:
            log.info("dreaming_io_tokens: used=%d, period=%s", usage_row["used"], usage_row["period_start"])
            phase2_io = phase2.get("io_tokens", 0)
            log.info("Phase 2 reported io_tokens: %d", phase2_io)
            log.info("Match: %s", usage_row["used"] == phase2_io)
        else:
            log.info("No usage_tracking row found!")

        status_post = await check_quota(db, TEST_USER_ID, "dreaming_io_tokens", "free")
        log.info("Post-flight quota: used=%d, limit=%d, exceeded=%s",
                 status_post.used, status_post.limit, status_post.exceeded)

        # 9. Back-edges on targets
        log.info("\n=== BACK-EDGES ON TARGETS ===")
        for name in ("ml-report-chunk-0000", "arch-doc-chunk-0000", "session-ml-chunk-0", "session-arch-chunk-0"):
            rows = await db.fetch(
                "SELECT entity_type, graph_edges FROM kv_store WHERE entity_key = $1", name,
            )
            if rows:
                edges = ensure_parsed(rows[0]["graph_edges"], default=[])
                dreamed = [e for e in (edges or []) if e.get("relation") == "dreamed_from"]
                if dreamed:
                    log.info("  %s has %d dreamed_from back-edge(s)", name, len(dreamed))

        # 10. Write YAML output
        output_path = "/tmp/example-full-dreaming.yaml"
        log.info("\n=== WRITING YAML OUTPUT ===")
        chunk_out = []
        for c in chunks:
            meta = ensure_parsed(c["metadata"], default={}) or {}
            chunk_out.append({
                "name": c["name"],
                "summary": c["summary"],
                "resource_keys": meta.get("resource_keys", []) if isinstance(meta, dict) else [],
            })
        dream_out = []
        for d in dreams:
            dream_out.append({k: _clean(d[k]) for k in d.keys()})
        back_edges_out: dict = {}
        for name in ("ml-report-chunk-0000", "arch-doc-chunk-0000"):
            be_rows = await db.fetch(
                "SELECT entity_type, graph_edges FROM kv_store WHERE entity_key = $1", name,
            )
            if be_rows:
                edges = ensure_parsed(be_rows[0]["graph_edges"], default=[])
                dreamed = [e for e in (edges or []) if e.get("relation") == "dreamed_from"]
                if dreamed:
                    back_edges_out[name] = dreamed
        out = {
            "run": {
                "user_id": str(TEST_USER_ID),
                "phase1": {
                    "sessions_checked": phase1.get("sessions_checked", 0),
                    "session_chunks_built": phase1.get("moments_built", 0),
                },
                "phase2": {
                    "dream_moments_saved": phase2.get("moments_saved", 0),
                    "io_tokens": phase2.get("io_tokens", 0),
                    "session_id": phase2.get("session_id"),
                },
            },
            "session_chunks": chunk_out,
            "dream_moments": dream_out,
            "back_edges": back_edges_out,
        }
        if usage_row:
            out["cost"] = {
                "dreaming_io_tokens": usage_row["used"],
                "period": str(usage_row["period_start"]),
            }
        with open(output_path, "w") as f:
            yaml.dump(out, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
        log.info("Wrote %s", output_path)

        log.info("\n=== DONE ===")


def register_dream_command(app: typer.Typer):
    """Register the dream command directly on the main app."""

    @app.command()
    def dream(
        user_id: Optional[str] = typer.Argument(None, help="User UUID to run dreaming for"),
        lookback_days: int = typer.Option(1, "--lookback", "-l", help="Date window in days"),
        allow_empty: bool = typer.Option(False, "--allow-empty", "-e", help="Exploration mode even with no activity"),
        output: Optional[str] = typer.Option(None, "--output", "-o", help="Write full results to YAML file"),
        simulate: bool = typer.Option(False, "--simulate", "-s", help="Seed fixture data and run full test harness"),
    ):
        """Run dreaming for a user — consolidation + AI insights."""
        if simulate:
            asyncio.run(_simulate_dream())
        elif user_id:
            asyncio.run(_run_dream(UUID(user_id), lookback_days, allow_empty, output))
        else:
            typer.echo("Error: USER_ID is required (or use --simulate)", err=True)
            raise typer.Exit(1)
