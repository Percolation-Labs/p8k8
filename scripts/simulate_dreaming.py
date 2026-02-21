"""Simulate a full dreaming run: seed data → run handler → inspect results.

Runs both phases of dreaming:
  Phase 1: rem_build_moment() consolidation (SQL only)
  Phase 2: LLM agent with structured output → DreamMoment objects persisted directly

Usage:
    uv run python scripts/simulate_dreaming.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from uuid import UUID

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s  %(message)s")
log = logging.getLogger("simulate")

# Silence noisy loggers
for name in ("httpx", "httpcore", "openai", "pydantic_ai"):
    logging.getLogger(name).setLevel(logging.WARNING)

TEST_USER_ID = UUID("dddddddd-0000-0000-0000-000000000001")


async def main():
    from p8.services.bootstrap import _export_api_keys
    from p8.services.database import Database
    from p8.services.encryption import EncryptionService
    from p8.services.kms import LocalFileKMS
    from p8.settings import Settings
    from p8.api.tools import init_tools, set_tool_context
    from p8.workers.handlers.dreaming import DreamingHandler

    settings = Settings()
    _export_api_keys(settings)

    db = Database(settings)
    await db.connect()

    kms = LocalFileKMS(settings.kms_local_keyfile, db)
    enc = EncryptionService(kms, system_tenant_id=settings.system_tenant_id, cache_ttl=settings.dek_cache_ttl)
    await enc.ensure_system_key()

    # ── 1. Clean prior dreaming state ──
    log.info("=== CLEANUP ===")
    await db.execute("DELETE FROM moments WHERE moment_type = 'dream' AND user_id = $1", TEST_USER_ID)
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

    # ── 2. Seed fixture data ──
    log.info("\n=== SEED FIXTURES ===")
    from tests.integration.dreaming.fixtures import setup_dreaming_fixtures
    sa, sb, ma, mb, ra, rb = await setup_dreaming_fixtures(db, enc)
    log.info("Created sessions: %s, %s", sa.name, sb.name)
    log.info("Created moments:  %s, %s", ma.name, mb.name)
    log.info("Created resources: %s, %s", ra.name, rb.name)

    # ── 3. Show pre-flight state ──
    log.info("\n=== PRE-FLIGHT STATE ===")
    from p8.services.usage import check_quota
    status = await check_quota(db, TEST_USER_ID, "dreaming_io_tokens", "free")
    log.info("dreaming_io_tokens quota: used=%d, limit=%d, exceeded=%s", status.used, status.limit, status.exceeded)

    status_min = await check_quota(db, TEST_USER_ID, "dreaming_minutes", "free")
    log.info("dreaming_minutes quota:   used=%d, limit=%d, exceeded=%s", status_min.used, status_min.limit, status_min.exceeded)

    # ── 4. Run DreamingHandler ──
    log.info("\n=== RUNNING DREAMING HANDLER ===")
    init_tools(db, enc)
    set_tool_context(user_id=TEST_USER_ID)

    class Ctx:
        pass
    ctx = Ctx()
    ctx.db = db
    ctx.encryption = enc

    handler = DreamingHandler()

    # First show context loading
    text, stats = await handler._load_dreaming_context(TEST_USER_ID, lookback_days=1, db=db, encryption=enc)
    log.info("Context loaded: %d chars", len(text))
    log.info("Context stats: %s", json.dumps(stats, indent=2))
    log.info("Context preview (first 500 chars):\n%s", text[:500])

    # Now run the full handler
    result = await handler.handle({"user_id": str(TEST_USER_ID), "lookback_days": 1}, ctx)
    log.info("\n=== HANDLER RESULT ===")
    log.info("Total io_tokens: %d", result.get("io_tokens", 0))
    log.info("Phase 1: %s", json.dumps(result.get("phase1", {}), indent=2))
    log.info("Phase 2: %s", json.dumps(result.get("phase2", {}), indent=2, default=str))

    # ── 5. Inspect what was created ──
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

        # Messages in the session
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

    # ── 6. Dream moments created ──
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
        tags = d["topic_tags"]
        if isinstance(tags, str):
            tags = json.loads(tags)
        log.info("  topic_tags: %s", tags)
        emotions = d["emotion_tags"]
        if isinstance(emotions, str):
            emotions = json.loads(emotions)
        log.info("  emotion_tags: %s", emotions)
        edges = d["graph_edges"]
        if isinstance(edges, str):
            edges = json.loads(edges)
        log.info("  graph_edges (%d):", len(edges) if edges else 0)
        for e in (edges or []):
            log.info("    → %s [%s] weight=%.1f reason=%s",
                     e.get("target"), e.get("relation"), e.get("weight", 0), e.get("reason", ""))

    # ── 7. Usage tracking ──
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

    # Post-flight quota check
    status_post = await check_quota(db, TEST_USER_ID, "dreaming_io_tokens", "free")
    log.info("Post-flight quota: used=%d, limit=%d, exceeded=%s",
             status_post.used, status_post.limit, status_post.exceeded)

    # ── 8. Back-edges on targets ──
    log.info("\n=== BACK-EDGES ON TARGETS ===")
    for name in ("ml-report-chunk-0000", "arch-doc-chunk-0000", "session-ml-chunk-0", "session-arch-chunk-0"):
        rows = await db.fetch(
            "SELECT entity_type, graph_edges FROM kv_store WHERE entity_key = $1", name,
        )
        if rows:
            edges = rows[0]["graph_edges"]
            if isinstance(edges, str):
                edges = json.loads(edges)
            dreamed = [e for e in (edges or []) if e.get("relation") == "dreamed_from"]
            if dreamed:
                log.info("  %s has %d dreamed_from back-edge(s)", name, len(dreamed))

    await db.close()
    log.info("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
