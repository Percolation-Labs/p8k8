"""Dreaming handler — per-user background AI processing.

Loads recent messages, moments, and resources as context, then runs the
dreaming agent which produces structured DreamMoment insights.

Persistence is handled by the chained_tool mechanism: the dreaming agent
schema declares `chained_tool: save_moments`, so the adapter automatically
pipes structured output into the save_moments tool after the agent run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from p8.ontology.types import Message, Moment, Session
from p8.services.database import Database
from p8.services.encryption import EncryptionService
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

# Phase 1 constants
MAX_SESSIONS_PHASE1 = 10
PHASE1_THRESHOLD = 6000
MAX_RESOURCE_SUMMARY_CHARS = 500


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

        # Phase 1 — consolidate recent sessions into session_chunk moments
        phase1 = await self._build_session_moments(user_id, tenant_id, ctx.db, ctx.encryption)
        log.info(
            "Phase 1 complete for user %s: %d moments built from %d sessions",
            user_id, phase1["moments_built"], phase1["sessions_checked"],
        )

        # Phase 2 — run dreaming agent(s) from tenant config
        result = await self._run_dreaming_agent(user_id, lookback_days, ctx, tenant_id=tenant_id)

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
            "phase1": phase1,
            "phase2": result,
        }

    # ------------------------------------------------------------------
    # Phase 1 — session consolidation + resource enrichment
    # ------------------------------------------------------------------

    async def _build_session_moments(
        self,
        user_id: UUID,
        tenant_id: str | None,
        db: Database,
        encryption: EncryptionService,
    ) -> dict:
        """Find recent sessions and build session_chunk moments for each.

        Returns {"sessions_checked": int, "moments_built": int}.
        """
        rows = await db.fetch(
            "SELECT id FROM sessions"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND COALESCE(mode, '') != 'dreaming'"
            " ORDER BY updated_at DESC LIMIT $2",
            user_id, MAX_SESSIONS_PHASE1,
        )

        memory = MemoryService(db, encryption)
        sessions_checked = 0
        moments_built = 0

        for row in rows:
            session_id = row["id"]
            sessions_checked += 1
            try:
                moment = await memory.maybe_build_moment(
                    session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    threshold=PHASE1_THRESHOLD,
                )
                if moment:
                    moments_built += 1
                    await self._enrich_moment_with_resources(db, moment, session_id)
            except Exception:
                log.exception("Phase 1: failed to build moment for session %s", session_id)

        return {"sessions_checked": sessions_checked, "moments_built": moments_built}

    @staticmethod
    async def _enrich_moment_with_resources(
        db: Database, moment: Moment, session_id: UUID,
    ) -> None:
        """Append uploaded resource content to a session_chunk moment.

        Looks up content_upload moments for the session, extracts chunk-0000
        resource keys, queries the resources table for content, and appends
        a summary to the moment.
        """
        upload_rows = await db.fetch(
            "SELECT metadata FROM moments"
            " WHERE source_session_id = $1"
            "   AND moment_type = 'content_upload'"
            "   AND deleted_at IS NULL",
            session_id,
        )
        if not upload_rows:
            return

        resource_keys: list[str] = []
        for urow in upload_rows:
            meta = ensure_parsed(urow["metadata"], default={})
            if not isinstance(meta, dict):
                continue
            for key in meta.get("resource_keys", []):
                if key.endswith("-chunk-0000") and key not in resource_keys:
                    resource_keys.append(key)

        if not resource_keys:
            return

        # Fetch content for chunk-0000 resources
        resource_rows = await db.fetch(
            "SELECT name, content FROM resources"
            " WHERE name = ANY($1) AND deleted_at IS NULL",
            resource_keys,
        )
        if not resource_rows:
            return

        parts: list[str] = []
        for rrow in resource_rows:
            content = rrow["content"] or ""
            if len(content) > MAX_RESOURCE_SUMMARY_CHARS:
                content = content[:MAX_RESOURCE_SUMMARY_CHARS] + "..."
            parts.append(f"- {rrow['name']}: {content}")

        resource_section = "\n\n[Uploaded Resources]\n" + "\n".join(parts)

        # Append to moment summary and merge resource_keys into metadata
        existing_meta = ensure_parsed(moment.metadata, default={})
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        existing_meta["resource_keys"] = resource_keys

        await db.execute(
            "UPDATE moments SET summary = summary || $1, metadata = $2::jsonb"
            " WHERE id = $3",
            resource_section,
            json.dumps(existing_meta),
            moment.id,
        )

    # ------------------------------------------------------------------
    # Phase 2 — Dreaming agent
    # ------------------------------------------------------------------

    async def _resolve_dreamer_agents(self, db, user_id: UUID, tenant_id: str | None) -> list[str]:
        """Resolve which dreamer agent schemas to run for this user.

        Checks tenant metadata for a custom dreamer_agents list.
        Falls back to ["dreaming-agent"] if no tenant config found.
        """
        # Try to resolve tenant_id from the user if not already provided
        effective_tenant = tenant_id
        if not effective_tenant:
            row = await db.fetchrow(
                "SELECT tenant_id FROM users WHERE (id = $1 OR user_id = $1) AND deleted_at IS NULL",
                user_id,
            )
            if row:
                effective_tenant = row["tenant_id"]

        if effective_tenant:
            tenant_row = await db.fetchrow(
                "SELECT metadata FROM tenants WHERE name = $1 AND deleted_at IS NULL",
                effective_tenant,
            )
            if tenant_row and tenant_row["metadata"]:
                from p8.ontology.types import TenantMetadata
                raw = ensure_parsed(tenant_row["metadata"], default={})
                meta = TenantMetadata(**(raw if isinstance(raw, dict) else {}))
                if meta.dreamer_agents:
                    log.info("Tenant %s has custom dreamers: %s", effective_tenant, meta.dreamer_agents)
                    return meta.dreamer_agents

        return ["dreaming-agent"]

    async def _run_dreaming_agent(
        self, user_id: UUID, lookback_days: int, ctx, *, tenant_id: str | None = None,
    ) -> dict:
        try:
            context_text, stats = await self._load_dreaming_context(
                user_id, lookback_days, ctx.db, ctx.encryption,
            )
            if not context_text.strip():
                return {"status": "skipped_no_data", "io_tokens": 0}

            agent_names = await self._resolve_dreamer_agents(ctx.db, user_id, tenant_id)

            total_io_tokens = 0
            total_moments_saved = 0
            session_ids = []

            for agent_name in agent_names:
                try:
                    result = await self._run_single_dreamer(
                        agent_name, user_id, lookback_days, context_text, stats, ctx,
                    )
                    total_io_tokens += result.get("io_tokens", 0)
                    total_moments_saved += result.get("moments_saved", 0)
                    if result.get("session_id"):
                        session_ids.append(result["session_id"])
                except Exception:
                    log.exception("Dreamer %s failed for user %s", agent_name, user_id)

            return {
                "status": "ok",
                "io_tokens": total_io_tokens,
                "session_id": session_ids[0] if session_ids else "",
                "session_ids": session_ids,
                "moments_saved": total_moments_saved,
                "context_stats": stats,
                "agents_run": agent_names,
            }

        except Exception as e:
            log.exception("Dreaming agent failed for user %s", user_id)
            return {"status": "error", "error": str(e), "io_tokens": 0}

    async def _run_single_dreamer(
        self, agent_name: str, user_id: UUID, lookback_days: int,
        context_text: str, stats: dict, ctx,
    ) -> dict:
        """Run a single dreamer agent and persist its output."""
        session_id = uuid4()
        session_repo = Repository(Session, ctx.db, ctx.encryption)
        dreaming_session = Session(
            id=session_id,
            name=f"dreaming-{user_id}",
            agent_name=agent_name,
            mode="dreaming",
            user_id=user_id,
        )
        await session_repo.upsert(dreaming_session)

        from p8.api.tools import init_tools, set_tool_context
        init_tools(ctx.db, ctx.encryption)
        set_tool_context(user_id=user_id, session_id=session_id)

        from p8.agentic.adapter import AgentAdapter
        adapter = await AgentAdapter.from_schema_name(
            agent_name, ctx.db, ctx.encryption, user_id=user_id,
        )

        agent = adapter.build_agent()
        injector = adapter.build_injector(user_id=user_id)

        prompt = (
            f"## Recent Activity (last {lookback_days} day(s))\n\n"
            f"{context_text}\n\n"
            "This is NEW activity only — no prior dreams are included. "
            "Focus your synthesis on what happened in these sessions, "
            "moments, and uploads. Use first-order dreaming to find "
            "insights across this fresh material, then second-order "
            "dreaming to lightly search for adjacent connections in the "
            "knowledge base. Keep affinity links sparse — only add them "
            "when a connection is genuinely surprising or useful. "
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

        io_tokens = 0
        if hasattr(result, "usage"):
            u = result.usage()
            io_tokens = u.total_tokens

        # Convert structured output to dict for chained tool
        moments_saved = 0
        output = result.output
        if hasattr(output, "dream_moments") and output.dream_moments:
            structured = {"moments": [
                dm.model_dump() if hasattr(dm, "model_dump") else dm
                for dm in output.dream_moments
            ]}
            chained_result = await adapter.execute_chained_tool(
                structured,
                session_id=session_id,
                user_id=user_id,
            )
            if chained_result and chained_result.get("status") == "success":
                moments_saved = chained_result.get("moments_count", 0)

        return {
            "status": "ok",
            "io_tokens": io_tokens,
            "session_id": str(session_id),
            "moments_saved": moments_saved,
        }

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

        # 1. Recent moments — exclude dreams and session_chunks to avoid echo chamber
        moment_rows = await db.fetch(
            "SELECT * FROM moments"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND created_at >= $2"
            "   AND moment_type NOT IN ('dream', 'session_chunk')"
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

                lines.append(
                    f"### {name} ({mtype})\n"
                    f"{summary}\n"
                    f"Tags: {', '.join(tags) if tags else 'none'}\n"
                )
                stats["moments"] += 1

            section = "\n".join(lines)
            section_tokens = estimate_tokens(section)
            if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                sections.append(section)
                token_estimate += section_tokens

        # 2. Recent session messages — exclude dreaming sessions
        session_rows = await db.fetch(
            "SELECT id, name, agent_name FROM sessions"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND updated_at >= $2"
            "   AND COALESCE(mode, '') != 'dreaming'"
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

        # 3. Recent file uploads
        file_rows = await db.fetch(
            "SELECT id, name, mime_type, parsed_content, size_bytes, created_at"
            " FROM files"
            " WHERE user_id = $1 AND deleted_at IS NULL"
            "   AND processing_status = 'completed'"
            "   AND created_at >= $2"
            " ORDER BY created_at DESC LIMIT $3",
            user_id, cutoff, MAX_RESOURCES,
        )
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

            section = "\n".join(lines)
            section_tokens = estimate_tokens(section)
            if token_estimate + section_tokens <= DATA_TOKEN_BUDGET:
                sections.append(section)
                token_estimate += section_tokens

        stats["token_estimate"] = token_estimate
        return "\n\n".join(sections), stats
