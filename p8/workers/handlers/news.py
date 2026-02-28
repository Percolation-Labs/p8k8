"""News feed handler — daily feed digest via p8-platoon.

Fetches news sources, scores items against the user's interests/categories
(from UserMetadata), and upserts the results as Resources + a digest Moment.

TODO: upsert is bulk by default - test with this instead of looping

"""

from __future__ import annotations

import logging
from uuid import UUID

from p8.ontology.types import UserMetadata
from p8.services.repository import Repository
from p8.ontology.types import Resource, Moment

log = logging.getLogger(__name__)


class NewsHandler:
    """Background handler: produce a daily news digest for a user."""

    async def handle(self, task: dict, ctx) -> dict:
        user_id = task.get("user_id")
        if not user_id:
            return {"status": "skipped_no_user", "resources": 0}

        if isinstance(user_id, str):
            user_id = UUID(user_id)

        log.info("News digest for user %s", user_id)

        # ── 1. Load user metadata ────────────────────────────────
        row = await ctx.db.fetchrow(
            "SELECT metadata FROM users WHERE (id = $1 OR user_id = $1) AND deleted_at IS NULL",
            user_id,
        )
        if not row or not row["metadata"]:
            log.warning("No metadata for user %s, skipping news", user_id)
            return {"status": "skipped_no_metadata", "resources": 0}

        from p8.utils.parsing import ensure_parsed
        raw_meta = ensure_parsed(row["metadata"], default={})
        user_metadata = UserMetadata(**(raw_meta if isinstance(raw_meta, dict) else {}))

        if not user_metadata.interests and not user_metadata.categories:
            log.warning("User %s has no interests/categories, skipping", user_id)
            return {"status": "skipped_no_interests", "resources": 0}

        # ── 2. Run platoon ────────────────────────────────────────
        import os
        from platoon.config import resolve_for_user
        from platoon.providers import FeedProvider

        # Bridge P8_TAVILY_API_KEY → P8_TAVILY_KEY so platoon picks it up.
        # Settings uses P8_TAVILY_API_KEY; platoon checks P8_TAVILY_KEY.
        tavily_key = (
            os.environ.get("P8_TAVILY_KEY")
            or os.environ.get("P8_TAVILY_API_KEY")
            or (getattr(ctx, "settings", None) and ctx.settings.tavily_api_key)
            or ""
        )

        from p8.workers.handlers.reading import _build_user_sources

        user_sources = _build_user_sources(user_metadata)
        pipeline_config = resolve_for_user(user_metadata, config=user_sources)
        provider = FeedProvider(tavily_key=tavily_key or None)
        result = provider.run(pipeline_config, user_id=user_id)

        log.info(
            "Platoon returned %d resources, %d moments for user %s (custom_sources=%s)",
            len(result.resources), len(result.moments), user_id, user_sources is not None,
        )

        if not result.resources:
            return {"status": "ok", "resources": 0, "moments": 0}

        # ── 3. Upsert resources ───────────────────────────────────
        resource_repo = Repository(Resource, ctx.db, ctx.encryption)
        moment_repo = Repository(Moment, ctx.db, ctx.encryption)

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

        # ── 4. Upsert digest moment ──────────────────────────────
        moments_saved = 0
        for p8m in result.moments:
            moment_entity = Moment(
                id=p8m.id,
                name=p8m.name,
                moment_type=p8m.moment_type,
                summary=p8m.summary,
                user_id=user_id,
                tags=p8m.tags,
                graph_edges=p8m.graph_edges,
                metadata=p8m.metadata,
            )
            try:
                await moment_repo.upsert(moment_entity)
                moments_saved += 1
            except Exception:
                log.exception("Failed to upsert moment %s", p8m.name)

        # ── 5. Track usage ────────────────────────────────────────
        try:
            from p8.services.usage import get_user_plan, increment_usage
            plan_id = await get_user_plan(ctx.db, user_id)
            await increment_usage(ctx.db, user_id, "news_searches_daily", 1, plan_id)
        except Exception:
            log.exception("Failed to record news usage for user %s", user_id)

        log.info(
            "News digest complete: %d resources, %d moments for user %s",
            resources_saved, moments_saved, user_id,
        )

        return {
            "status": "ok",
            "resources": resources_saved,
            "moments": moments_saved,
        }
