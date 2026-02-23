"""Dreaming handler — per-user background AI processing.

Loads recent messages, moments, and resources as context, then runs the
dreaming agent which produces structured DreamMoment insights.

The dreaming agent uses structured output to return DreamMoment objects
directly. The handler persists them to the database and merges back-edges
onto referenced entities — no save_moments tool call needed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from p8.ontology.types import Message, Moment, Session
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.graph import merge_graph_edges
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.parsing import ensure_parsed
from p8.utils.tokens import estimate_tokens

log = logging.getLogger(__name__)

# Token budget constants (gpt-4.1-mini = 128K context)
CONTEXT_BUDGET_RATIO = 0.30
MODEL_CONTEXT_LIMIT = 128_000
DATA_TOKEN_BUDGET = int(MODEL_CONTEXT_LIMIT * CONTEXT_BUDGET_RATIO)  # ~38K tokens
MAX_RESOURCE_CHARS = 2000
MAX_MOMENTS = 50
MAX_MESSAGES_PER_SESSION = 20
MAX_RESOURCES = 10
LOOKBACK_DAYS = 1


class DreamingHandler:
    """Background AI processing for a user — moment consolidation and insights."""

    async def handle(self, task: dict, ctx) -> dict:
        user_id = task.get("user_id")
        if not user_id:
            return {"io_tokens": 0, "status": "skipped_no_user"}

        if isinstance(user_id, str):
            user_id = UUID(user_id)

        log.info("Dreaming for user %s", user_id)
        tenant_id = task.get("tenant_id")
        lookback_days = task.get("lookback_days", LOOKBACK_DAYS)

        # Run dreaming agent (loads messages + moments directly as context)
        result = await self._run_dreaming_agent(user_id, lookback_days, ctx)

        io_tokens = result.get("io_tokens", 0)
        log.info(
            "Dreaming complete for user %s: status=%s tokens=%d",
            user_id, result.get("status"), io_tokens,
        )

        # Record actual LLM token consumption against user's plan quota
        if io_tokens > 0:
            try:
                from p8.services.usage import get_user_plan, increment_usage

                plan_id = await get_user_plan(ctx.db, user_id)
                await increment_usage(ctx.db, user_id, "dreaming_io_tokens", io_tokens, plan_id)
            except Exception:
                log.exception("Failed to record dreaming usage for user %s", user_id)

        return {
            "io_tokens": io_tokens,
            "phase2": result,
        }

    # ------------------------------------------------------------------
    # Dreaming agent
    # ------------------------------------------------------------------

    async def _run_dreaming_agent(
        self, user_id: UUID, lookback_days: int, ctx,
    ) -> dict:
        try:
            context_text, stats = await self._load_dreaming_context(
                user_id, lookback_days, ctx.db, ctx.encryption,
            )
            if not context_text.strip():
                return {"status": "skipped_no_data", "io_tokens": 0}

            # Create a session for this dreaming run
            session_id = uuid4()
            session_repo = Repository(Session, ctx.db, ctx.encryption)
            dreaming_session = Session(
                id=session_id,
                name=f"dreaming-{user_id}",
                agent_name="dreaming-agent",
                mode="dreaming",
                user_id=user_id,
            )
            await session_repo.upsert(dreaming_session)

            # Ensure MCP tools have DB/encryption/session access in worker context
            from p8.api.tools import init_tools, set_tool_context
            init_tools(ctx.db, ctx.encryption)
            set_tool_context(user_id=user_id, session_id=session_id)

            from p8.agentic.adapter import AgentAdapter
            adapter = await AgentAdapter.from_schema_name(
                "dreaming-agent", ctx.db, ctx.encryption, user_id=user_id,
            )

            agent = adapter.build_agent()
            injector = adapter.build_injector(user_id=user_id)

            prompt = (
                f"## Recent Activity (last {lookback_days} day(s))\n\n"
                f"{context_text}\n\n"
                "Reflect on this shared activity. Use first-order dreaming to "
                "consolidate themes, then second-order dreaming to search for "
                "semantic connections across the full knowledge base. "
                "Populate your structured output with the results."
            )

            result = await agent.run(
                prompt,
                instructions=injector.instructions,
                usage_limits=(
                    adapter.config.limits.to_pydantic_ai()
                    if adapter.config.limits else None
                ),
            )

            # Extract actual API token usage (not estimates)
            io_tokens = 0
            if hasattr(result, "usage"):
                u = result.usage()
                io_tokens = u.total_tokens

            # Extract structured output and persist dream moments.
            # Each dream moment gets a companion session via create_moment_session()
            # so users can chat about individual insights.
            moments_saved = 0
            output = result.output
            memory = MemoryService(ctx.db, ctx.encryption)
            if hasattr(output, "dream_moments") and output.dream_moments:
                moments_saved = await self._persist_dream_moments(
                    output.dream_moments, user_id, session_id, ctx, memory,
                )

            return {
                "status": "ok",
                "io_tokens": io_tokens,
                "session_id": str(session_id),
                "moments_saved": moments_saved,
                "context_stats": stats,
            }

        except Exception as e:
            log.exception("Dreaming agent failed for user %s", user_id)
            return {"status": "error", "error": str(e), "io_tokens": 0}

    # ------------------------------------------------------------------
    # Persist structured output → Moment entities + back-edges
    # ------------------------------------------------------------------

    async def _persist_dream_moments(
        self,
        dream_moments: list,
        user_id: UUID,
        dreaming_session_id: UUID,
        ctx,
        memory: MemoryService,
    ) -> int:
        """Persist DreamMoment structured output to the database.

        For each dream moment:
        1. Convert affinity_fragments → graph_edges
        2. Create a Moment + companion session via create_moment_session()
        3. Merge back-edges onto referenced entities (bidirectional links)

        Returns the number of moments saved.
        """
        saved = 0

        for dm in dream_moments:
            try:
                # Convert structured affinity_fragments to graph_edges dicts
                affinities = dm.affinity_fragments if hasattr(dm, "affinity_fragments") else []
                graph_edges = [
                    {
                        "target": a.target if hasattr(a, "target") else a.get("target", ""),
                        "relation": (a.relation if hasattr(a, "relation") else a.get("relation", "dream_affinity")),
                        "weight": (a.weight if hasattr(a, "weight") else a.get("weight", 0.5)),
                        "reason": (a.reason if hasattr(a, "reason") else a.get("reason", "")),
                    }
                    for a in affinities
                    if (a.target if hasattr(a, "target") else a.get("target"))
                ]

                # Convert kebab-case name to Title Case for display
                raw_name = dm.name if hasattr(dm, "name") else dm.get("name", "unnamed")
                # Strip any dream- prefix (redundant — moment_type is already "dream")
                if raw_name.startswith("dream-"):
                    raw_name = raw_name[6:]
                name = raw_name.replace("-", " ").title()

                summary = dm.summary if hasattr(dm, "summary") else dm.get("summary", "")
                topic_tags = dm.topic_tags if hasattr(dm, "topic_tags") else dm.get("topic_tags", [])
                emotion_tags = dm.emotion_tags if hasattr(dm, "emotion_tags") else dm.get("emotion_tags", [])

                saved_moment, _session = await memory.create_moment_session(
                    name=name,
                    moment_type="dream",
                    summary=summary,
                    metadata={"source": "dreaming"},
                    user_id=user_id,
                    topic_tags=topic_tags,
                    graph_edges=graph_edges,
                )

                # Patch emotion_tags (not in create_moment_session)
                if emotion_tags:
                    await ctx.db.execute(
                        "UPDATE moments SET emotion_tags = $1::text[] WHERE id = $2",
                        emotion_tags, saved_moment.id,
                    )

                saved += 1

                # Merge back-edges on referenced entities
                for edge in graph_edges:
                    target_key = edge.get("target")
                    if not target_key:
                        continue
                    back_edge = {
                        "target": saved_moment.name,
                        "relation": "dreamed_from",
                        "weight": edge.get("weight", 0.5),
                        "reason": edge.get("reason", ""),
                    }
                    try:
                        await self._merge_edge_on_target(ctx.db, target_key, back_edge)
                    except Exception:
                        log.warning("Failed to merge back-edge on %s", target_key, exc_info=True)

            except Exception:
                log.exception("Failed to persist dream moment: %s", getattr(dm, "name", dm))

        return saved

    _ALLOWED_ENTITY_TABLES = frozenset({
        "resources", "moments", "files", "schemas", "sessions",
    })

    @staticmethod
    async def _merge_edge_on_target(db, target_key: str, new_edge: dict) -> None:
        """Look up entity by key via kv_store index, merge a back-edge onto the source table.

        kv_store is UNLOGGED and ephemeral — only used here to resolve
        (entity_type, entity_id) from the key.  The actual graph_edges are
        read from and written to the source table so they survive rebuilds.
        kv_store is refreshed via entity table triggers (or rebuild_kv_store()).
        """
        # Resolve key → source table + id via the KV index
        rows = await db.fetch(
            "SELECT entity_type, entity_id FROM kv_store"
            " WHERE entity_key = $1 LIMIT 1",
            target_key,
        )
        if not rows:
            return

        entity_type = rows[0]["entity_type"]
        entity_id = rows[0]["entity_id"]

        # Validate entity_type against known tables to prevent SQL injection
        if entity_type not in DreamingHandler._ALLOWED_ENTITY_TABLES:
            log.warning("Unexpected entity_type %r from kv_store for key %s, skipping", entity_type, target_key)
            return

        # Read current graph_edges from the SOURCE table (authoritative)
        source_rows = await db.fetch(
            f"SELECT graph_edges FROM {entity_type} WHERE id = $1",
            entity_id,
        )
        if not source_rows:
            return

        raw_edges = ensure_parsed(source_rows[0]["graph_edges"], default=[])
        existing_edges: list[dict] = raw_edges if isinstance(raw_edges, list) else []

        merged = merge_graph_edges(existing_edges, [new_edge])

        # Write back to the source table only — kv_store syncs from here
        await db.execute(
            f"UPDATE {entity_type} SET graph_edges = $1::jsonb WHERE id = $2",
            json.dumps(merged),
            entity_id,
        )

    # ------------------------------------------------------------------
    # Context loading
    # ------------------------------------------------------------------

    async def _load_dreaming_context(
        self,
        user_id: UUID,
        lookback_days: int,
        db: Database,
        encryption: EncryptionService,
    ) -> tuple[str, dict]:
        """Load moments, messages, and resources into a text context.

        Stays within DATA_TOKEN_BUDGET (~38K tokens).
        Returns (context_text, stats_dict).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        sections: list[str] = []
        token_estimate = 0
        stats = {"moments": 0, "sessions": 0, "messages": 0, "resources": 0}
        referenced_keys: set[str] = set()

        # 1. Recent moments
        moment_rows = await db.fetch(
            "SELECT * FROM moments"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND created_at >= $2"
            " ORDER BY created_at DESC LIMIT $3",
            user_id, cutoff, MAX_MOMENTS,
        )
        if moment_rows:
            lines = ["## Recent Moments\n"]
            for row in moment_rows:
                md = encryption.decrypt_fields(Moment, dict(row), None)
                name = md.get("name", "unnamed")
                summary = md.get("summary", "")
                mtype = md.get("moment_type", "")
                tags = md.get("topic_tags") or []
                edges = ensure_parsed(md.get("graph_edges"), default=[]) or []

                lines.append(
                    f"### {name} ({mtype})\n"
                    f"{summary}\n"
                    f"Tags: {', '.join(tags) if tags else 'none'}\n"
                )
                for edge in edges:
                    target = edge.get("target", "")
                    if target:
                        referenced_keys.add(target)
                stats["moments"] += 1

            section = "\n".join(lines)
            section_tokens = estimate_tokens(section)
            if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                sections.append(section)
                token_estimate += section_tokens

        # 2. Recent session messages
        session_rows = await db.fetch(
            "SELECT id, name, agent_name FROM sessions"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND updated_at >= $2"
            " ORDER BY updated_at DESC LIMIT 5",
            user_id, cutoff,
        )
        if session_rows:
            lines = ["## Recent Sessions\n"]
            for sess in session_rows:
                session_id = sess["id"]
                session_name = sess["name"] or "unnamed"
                stats["sessions"] += 1

                messages = await db.fetch(
                    "SELECT message_type, content, created_at FROM messages"
                    " WHERE session_id = $1 AND deleted_at IS NULL"
                    " ORDER BY created_at DESC LIMIT $2",
                    session_id, MAX_MESSAGES_PER_SESSION,
                )
                if messages:
                    lines.append(f"### Session: {session_name}\n")
                    for msg in reversed(messages):
                        md = encryption.decrypt_fields(Message, dict(msg), None)
                        content = md.get("content", "") or ""
                        mtype = md.get("message_type", "user")
                        if len(content) > 500:
                            content = content[:500] + "..."
                        lines.append(f"[{mtype}] {content}")
                        stats["messages"] += 1
                    lines.append("")

            section = "\n".join(lines)
            section_tokens = estimate_tokens(section)
            if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                sections.append(section)
                token_estimate += section_tokens

        # 3. Recent file uploads (direct query, not dependent on graph_edges)
        file_rows = await db.fetch(
            "SELECT id, name, mime_type, parsed_content, size_bytes, created_at"
            " FROM files"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND processing_status = 'completed'"
            "   AND created_at >= $2"
            " ORDER BY created_at DESC LIMIT $3",
            user_id, cutoff, MAX_RESOURCES,
        )
        seen_file_ids: set = set()
        if file_rows:
            lines = ["## Recent Uploads\n"]
            for frow in file_rows:
                content = frow["parsed_content"] or ""
                if len(content) > MAX_RESOURCE_CHARS:
                    content = content[:MAX_RESOURCE_CHARS] + "..."
                fname = frow["name"] or "unnamed"
                lines.append(
                    f"### {fname} ({frow['mime_type'] or 'unknown'})\n{content}\n"
                )
                stats["resources"] += 1
                seen_file_ids.add(frow["id"])

            section = "\n".join(lines)
            section_tokens = estimate_tokens(section)
            if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                sections.append(section)
                token_estimate += section_tokens

        # 4. Referenced resources (from moment graph_edges, skip already-loaded files)
        if referenced_keys:
            lines = ["## Referenced Resources\n"]
            looked_up = 0
            for key in list(referenced_keys)[:MAX_RESOURCES]:
                rows = await db.fetch(
                    "SELECT entity_type, entity_id, content_summary"
                    " FROM kv_store WHERE entity_key = $1 LIMIT 1",
                    key,
                )
                if not rows:
                    continue
                kv = rows[0]
                etype = kv["entity_type"]
                summary = kv["content_summary"] or ""

                # Skip files already loaded in section 3
                if etype == "files" and kv["entity_id"] in seen_file_ids:
                    continue

                # For resources/files, try to load actual content
                _RESOURCE_QUERIES = {
                    "resources": "SELECT content FROM resources WHERE id = $1 AND deleted_at IS NULL LIMIT 1",
                    "files": "SELECT parsed_content FROM files WHERE id = $1 AND deleted_at IS NULL LIMIT 1",
                }
                if etype in _RESOURCE_QUERIES and kv["entity_id"]:
                    field = "content" if etype == "resources" else "parsed_content"
                    content_rows = await db.fetch(
                        _RESOURCE_QUERIES[etype],
                        kv["entity_id"],
                    )
                    if content_rows:
                        content = content_rows[0][field] or summary
                        if len(content) > MAX_RESOURCE_CHARS:
                            content = content[:MAX_RESOURCE_CHARS] + "..."
                        lines.append(f"### {key} ({etype})\n{content}\n")
                        looked_up += 1
                        stats["resources"] += 1
                elif summary:
                    lines.append(f"### {key} ({etype})\n{summary}\n")
                    looked_up += 1
                    stats["resources"] += 1

            if looked_up > 0:
                section = "\n".join(lines)
                section_tokens = estimate_tokens(section)
                if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                    sections.append(section)
                    token_estimate += section_tokens

        stats["token_estimate"] = token_estimate
        return "\n\n".join(sections), stats
