"""Reading pipeline handler — daily feed digest.

Handles both 'news' and 'reading_summary' task types (single pipeline).
The pg_cron job enqueues 'news' tasks; the worker dispatches them here.

1. Load user metadata (feeds, interests, categories)
2. Run platoon (resolve_for_user + FeedProvider)
3. Upsert resources
4. Build reading moment (one per day, date-based name)
5. Generate mosaic thumbnail
6. LLM summarize
7. Create companion session
8. Track usage
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from uuid import UUID

from p8.ontology.types import Moment, Resource, UserMetadata
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.utils.parsing import ensure_parsed

log = logging.getLogger(__name__)


def _build_user_sources(user_metadata: UserMetadata) -> dict | None:
    """Build custom platoon sources config from user metadata feeds/categories.

    Returns ``{"sources": {...}}`` for passing as ``config`` to
    ``resolve_for_user()``, or *None* to use platoon defaults.
    """
    # Direct sources override on UserMetadata takes full precedence
    if user_metadata.sources:
        return {"sources": user_metadata.sources}

    has_feeds = bool(user_metadata.feeds)
    has_categories = bool(user_metadata.categories)

    if not has_feeds and not has_categories:
        return None

    from platoon.config import DEFAULT_SOURCES

    # Shallow-copy each source dict so we don't mutate the library default
    sources = {k: ({**v} if isinstance(v, dict) else v) for k, v in DEFAULT_SOURCES.items()}

    # Replace default RSS with user's custom feeds
    if has_feeds:
        rss_feeds = []
        for feed in user_metadata.feeds or []:
            url = feed.get("url")
            if not url:
                continue
            rss_feeds.append({"url": url, "label": feed.get("name") or url})
        if rss_feeds:
            sources["rss_feeds"] = {
                "enabled": True,
                "feeds": rss_feeds,
                "max_items_per_feed": 3,
            }

    # Personalize google_news queries from category names
    if has_categories:
        queries = [f"{name} news" for name in (user_metadata.categories or {})]
        sources["google_news"] = {
            "enabled": True,
            "queries": queries,
            "max_items_per_query": 4,
        }
        # Disable tech-centric defaults when categories are custom
        sources["hacker_news"] = {"enabled": False}
        sources["reddit"] = {"enabled": False}

    return {"sources": sources}


SUMMARY_PROMPT = """\
You are summarizing a user's reading feed. Here are today's articles:

{items_text}

Write a 2-3 sentence summary weaving in the article links as markdown. \
The reader should be able to click through to anything that catches their eye. \
Focus on themes and what's interesting. Write in second person ("You have..."). \
Keep every markdown link from the input intact in your output."""


class ReadingSummaryHandler:
    """Background handler: full reading pipeline — fetch, ingest, summarize."""

    async def handle(self, task: dict, ctx) -> dict:
        user_id = task.get("user_id")
        if not user_id:
            return {"status": "skipped_no_user", "resources": 0}

        if isinstance(user_id, str):
            user_id = UUID(user_id)

        log.info("Reading pipeline for user %s", user_id)

        # ── 1. Load user metadata ────────────────────────────────
        row = await ctx.db.fetchrow(
            "SELECT metadata FROM users WHERE (id = $1 OR user_id = $1) AND deleted_at IS NULL",
            user_id,
        )
        if not row or not row["metadata"]:
            log.warning("No metadata for user %s, skipping reading", user_id)
            return {"status": "skipped_no_metadata", "resources": 0}

        raw_meta = ensure_parsed(row["metadata"], default={})
        user_metadata = UserMetadata(**(raw_meta if isinstance(raw_meta, dict) else {}))

        # ── 2. Run platoon (uses defaults if no feeds/interests) ──
        from platoon.config import resolve_for_user
        from platoon.providers import FeedProvider

        tavily_key = (
            os.environ.get("P8_TAVILY_KEY")
            or os.environ.get("P8_TAVILY_API_KEY")
            or (getattr(ctx, "settings", None) and ctx.settings.tavily_api_key)
            or ""
        )

        user_sources = _build_user_sources(user_metadata)
        pipeline_config = resolve_for_user(user_metadata, config=user_sources)
        provider = FeedProvider(tavily_key=tavily_key or None)
        result = provider.run(pipeline_config, user_id=user_id)

        log.info(
            "Platoon returned %d resources for user %s (custom_sources=%s)",
            len(result.resources), user_id, user_sources is not None,
        )

        if not result.resources:
            return {"status": "ok", "resources": 0}

        # ── 3. Upsert resources ───────────────────────────────────
        resource_repo = Repository(Resource, ctx.db, ctx.encryption)

        resources_saved = 0
        for p8r in result.resources:
            entity = Resource(
                id=p8r.id,
                name=p8r.name,
                uri=p8r.uri,
                content=p8r.content,
                category=p8r.category,
                image_uri=p8r.image_uri,
                related_entities=p8r.related_entities,
                user_id=user_id,
                tags=p8r.tags,
                metadata=p8r.metadata,
            )
            try:
                await resource_repo.upsert(entity)
                resources_saved += 1
            except Exception:
                log.exception("Failed to upsert resource %s", p8r.name[:60])

        # ── 4. Build reading moment ───────────────────────────────
        now = datetime.now(timezone.utc)
        moment_name = f"reading-{now.strftime('%Y-%m-%d')}"

        items = []
        all_tags: list[str] = []
        graph_edges: list[dict] = []
        for p8r in result.resources:
            items.append({
                "resource_id": str(p8r.id),
                "uri": p8r.uri or "",
                "title": p8r.name or "",
                "image_uri": p8r.image_uri or "",
                "tags": p8r.tags or [],
            })
            all_tags.extend(p8r.tags or [])
            graph_edges.append({
                "target": p8r.name,
                "relation": "contains",
                "weight": 1.0,
            })

        unique_tags = list(dict.fromkeys(all_tags))
        links = [
            {"title": i["title"], "url": i["uri"], "resource_id": i["resource_id"]}
            for i in items if i.get("uri")
        ]
        meta = {
            "source": "reading_pipeline",
            "resource_count": len(items),
            "items": items,
            "links": links,
        }

        # ── 5. Generate mosaic thumbnail ──────────────────────────
        image_uri = None
        try:
            from p8.services.content import generate_mosaic_thumbnail

            image_uris = [str(i.get("image_uri") or "") for i in items]
            image_uri = await generate_mosaic_thumbnail(image_uris)
        except Exception:
            log.warning("Reading mosaic generation failed", exc_info=True)

        # ── 6. LLM summarize ─────────────────────────────────────
        summary = await self._llm_summarize(items)
        if not summary:
            titles = [str(i["title"]) for i in items[:8]]
            summary = f"You have articles about: {', '.join(titles)}."

        # ── 7. Create moment + companion session ──────────────────
        memory = MemoryService(ctx.db, ctx.encryption)
        moment, session = await memory.create_moment_session(
            name=moment_name,
            moment_type="reading",
            summary=summary,
            metadata=meta,
            user_id=user_id,
            topic_tags=unique_tags,
            graph_edges=graph_edges,
            image_uri=image_uri,
            starts_timestamp=now,
        )

        log.info(
            "Reading moment %s created with %d items, session %s",
            moment.id, len(items), session.id,
        )

        # ── 8. Track usage ────────────────────────────────────────
        io_tokens = (len(summary) + sum(len(i.get("title", "")) for i in items)) // 4
        try:
            from p8.services.usage import get_user_plan, increment_usage
            plan_id = await get_user_plan(ctx.db, user_id)
            await increment_usage(ctx.db, user_id, "reading_summarize_io_tokens", io_tokens, plan_id)
        except Exception:
            log.exception("Failed to record reading usage for user %s", user_id)

        return {
            "status": "ok",
            "resources": resources_saved,
            "moment_id": str(moment.id),
            "session_id": str(session.id),
            "item_count": len(items),
            "io_tokens": io_tokens,
        }

    async def _llm_summarize(self, items: list[dict]) -> str | None:
        """Call a cheap model for summarization. Returns None on failure."""
        try:
            from pydantic_ai import Agent

            lines = []
            for item in items:
                title = item.get("title", "Untitled")
                uri = item.get("uri", "")
                tags = item.get("tags", [])
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                # Include URL as markdown link so the model can weave it into prose
                if uri:
                    lines.append(f"- [{title}]({uri}){tag_str}")
                else:
                    lines.append(f"- {title}{tag_str}")
            items_text = "\n".join(lines)

            agent = Agent(
                "openai:gpt-4.1-nano",
                instructions="You write concise reading summaries.",
            )
            prompt = SUMMARY_PROMPT.format(items_text=items_text)
            result = await agent.run(prompt)
            return str(result.output) if hasattr(result, "output") else str(getattr(result, "data", ""))
        except Exception:
            log.warning("LLM summarization failed, using fallback", exc_info=True)
            return None
