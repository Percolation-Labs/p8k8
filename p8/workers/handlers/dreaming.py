"""Dreaming handler — per-user background AI processing.

Phase 1: Build session_chunk moments via rem_build_moment() (SQL only, no LLM).
Phase 2: Run dreaming agent for cross-session insights (structured output).

The dreaming agent uses structured output to return DreamMoment objects
directly. The handler persists them to the database and merges back-edges
onto referenced entities — no save_moments tool call needed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

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

        # Phase 1: Build session chunk moments (existing behavior)
        phase1 = await self._build_session_moments(user_id, tenant_id, ctx)

        # Phase 2: Run dreaming agent
        phase2 = await self._run_dreaming_agent(user_id, lookback_days, ctx)

        total_tokens = phase1.get("io_tokens", 0) + phase2.get("io_tokens", 0)
        log.info(
            "Dreaming complete for user %s: phase1=%s phase2=%s tokens=%d",
            user_id, phase1.get("status"), phase2.get("status"), total_tokens,
        )

        # Post-flight: record actual LLM token consumption against user's plan quota.
        # Only Phase 2 has real API io_tokens; Phase 1 token counts are just
        # text estimates from rem_build_moment (no LLM calls).
        phase2_io = phase2.get("io_tokens", 0)
        if phase2_io > 0:
            try:
                from p8.services.usage import get_user_plan, increment_usage

                plan_id = await get_user_plan(ctx.db, user_id)
                await increment_usage(ctx.db, user_id, "dreaming_io_tokens", phase2_io, plan_id)
            except Exception:
                log.exception("Failed to record dreaming usage for user %s", user_id)

        return {
            "io_tokens": total_tokens,
            "phase1": phase1,
            "phase2": phase2,
        }

    # ------------------------------------------------------------------
    # Phase 1 — session chunk moments
    # ------------------------------------------------------------------

    async def _build_session_moments(
        self, user_id: UUID, tenant_id: str | None, ctx,
    ) -> dict:
        sessions = await ctx.db.fetch(
            "SELECT s.id, s.name, s.total_tokens, s.agent_name"
            " FROM sessions s"
            " WHERE s.user_id = $1 AND s.deleted_at IS NULL"
            " ORDER BY s.updated_at DESC LIMIT 10",
            user_id,
        )

        moments_built = 0
        total_io_tokens = 0

        for session in sessions:
            row = await ctx.db.fetchrow(
                "SELECT * FROM rem_build_moment($1, $2, $3, $4)",
                session["id"],
                tenant_id,
                user_id,
                6000,  # token threshold
            )
            if row and row["moment_id"]:
                moments_built += 1
                total_io_tokens += row.get("token_count", 0)

        return {
            "status": "ok",
            "io_tokens": total_io_tokens,
            "moments_built": moments_built,
            "sessions_checked": len(sessions),
        }

    # ------------------------------------------------------------------
    # Phase 2 — dreaming agent
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

            # Extract structured output and persist dream moments
            moments_saved = 0
            output = result.output
            if hasattr(output, "dream_moments") and output.dream_moments:
                moments_saved = await self._persist_dream_moments(
                    output.dream_moments, user_id, session_id, ctx,
                )

            # Persist all messages from the agent run into the session
            all_messages = (
                result.all_messages()
                if hasattr(result, "all_messages")
                else []
            )
            model_name = adapter.config.model or "openai:gpt-4.1-mini"
            memory = MemoryService(ctx.db, ctx.encryption)
            await self._persist_agent_messages(
                memory, session_id, all_messages, user_id,
                model=model_name, agent_name="dreaming-agent",
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
        session_id: UUID,
        ctx,
    ) -> int:
        """Persist DreamMoment structured output to the database.

        For each dream moment:
        1. Convert affinity_fragments → graph_edges
        2. Create a Moment entity and upsert it
        3. Merge back-edges onto referenced entities (bidirectional links)

        Returns the number of moments saved.
        """
        repo = Repository(Moment, ctx.db, ctx.encryption)
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

                # Ensure dream- prefix
                name = dm.name if hasattr(dm, "name") else dm.get("name", "unnamed")
                if not name.startswith("dream-"):
                    name = f"dream-{name}"

                moment = Moment(
                    name=name,
                    moment_type="dream",
                    summary=dm.summary if hasattr(dm, "summary") else dm.get("summary", ""),
                    topic_tags=dm.topic_tags if hasattr(dm, "topic_tags") else dm.get("topic_tags", []),
                    emotion_tags=dm.emotion_tags if hasattr(dm, "emotion_tags") else dm.get("emotion_tags", []),
                    graph_edges=graph_edges,
                    user_id=user_id,
                    source_session_id=session_id,
                    metadata={"source": "dreaming"},
                )
                [saved_moment] = await repo.upsert(moment)
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

    @staticmethod
    async def _merge_edge_on_target(db, target_key: str, new_edge: dict) -> None:
        """Look up entity by key via kv_store index, merge a back-edge onto the source table.

        kv_store is UNLOGGED and ephemeral — only used here to resolve
        (entity_type, entity_id) from the key.  The actual graph_edges are
        read from and written to the source table so they survive rebuilds.
        kv_store is refreshed on the next rem_sync_kv_store() run.
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

    async def _persist_agent_messages(
        self,
        memory: MemoryService,
        session_id: UUID,
        messages: list[ModelMessage],
        user_id: UUID,
        *,
        model: str = "",
        agent_name: str = "",
    ) -> None:
        """Persist pydantic-ai messages as individual rows in the dreaming session."""
        repo = memory.message_repo
        total_tokens = 0

        for msg in messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, SystemPromptPart):
                        continue  # system prompts live in the agent definition, not messages
                    elif isinstance(part, UserPromptPart):
                        content = part.content if isinstance(part.content, str) else str(part.content)
                        tc = estimate_tokens(content)
                        m = Message(
                            session_id=session_id, message_type="user",
                            content=content, token_count=tc,
                            user_id=user_id, agent_name=agent_name,
                        )
                        await repo.upsert(m)
                        total_tokens += tc
                    elif isinstance(part, ToolReturnPart):
                        content = part.content if isinstance(part.content, str) else json.dumps(part.content)
                        tc = estimate_tokens(content)
                        m = Message(
                            session_id=session_id, message_type="tool_call",
                            content=content, token_count=tc,
                            tool_calls={
                                "name": part.tool_name,
                                "id": part.tool_call_id or "",
                            },
                            user_id=user_id, model=model, agent_name=agent_name,
                        )
                        await repo.upsert(m)
                        total_tokens += tc

            elif isinstance(msg, ModelResponse):
                text_parts: list[str] = []
                tool_calls_data: list[dict] = []
                for part in msg.parts:  # type: ignore[assignment]  # ModelResponsePart, not request parts
                    if isinstance(part, TextPart):
                        text_parts.append(part.content)
                    elif isinstance(part, ToolCallPart):
                        # args can be str, dict, or None
                        args = part.args
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, TypeError):
                                args = {"raw": args}
                        elif args is None:
                            args = {}
                        tool_calls_data.append({
                            "name": part.tool_name,
                            "id": part.tool_call_id or "",
                            "arguments": args,
                        })
                content = "\n".join(text_parts) if text_parts else ""
                tc = estimate_tokens(content)
                tool_calls = {"calls": tool_calls_data} if tool_calls_data else None
                m = Message(
                    session_id=session_id, message_type="assistant",
                    content=content, token_count=tc, tool_calls=tool_calls,
                    user_id=user_id, model=model, agent_name=agent_name,
                )
                await repo.upsert(m)
                total_tokens += tc

        # Update session total_tokens once
        await memory.db.execute(
            "UPDATE sessions SET total_tokens = total_tokens + $1 WHERE id = $2",
            total_tokens, session_id,
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
