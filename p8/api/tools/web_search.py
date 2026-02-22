"""web_search tool — search the web via Tavily, save results as resources."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from p8.api.tools import get_db, get_encryption, get_session_id, get_user_id

logger = logging.getLogger(__name__)


async def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    save: bool = True,
) -> dict[str, Any]:
    """Search the web and return structured results.

    Calls the Tavily API, optionally saves results as Resource entities
    for later REM search, and creates a Moment to record the search event.

    Args:
        query: What to search for.
        max_results: Number of results (1-20, default 5).
        search_depth: "basic" (faster) or "advanced" (deeper).
        save: Whether to persist results as Resources (default True).

    Returns:
        Dict with status, results (card-shaped), count, moment_id, saved flag.
    """
    from p8.ontology.types import Moment, Resource
    from p8.services.repository import Repository
    from p8.services.usage import check_quota, get_user_plan, increment_usage
    from p8.services.web_search import search as tavily_search

    user_id = get_user_id()

    db = get_db()
    encryption = get_encryption()
    session_id = get_session_id()

    # Quota check (pre-flight)
    if user_id:
        plan_id = await get_user_plan(db, user_id)
        quota = await check_quota(db, user_id, "web_searches_daily", plan_id)
        if quota.exceeded:
            return {
                "status": "error",
                "error": f"Daily web search quota exceeded ({quota.used}/{quota.limit})",
                "query": query,
            }

    # Execute search
    try:
        raw_results = await tavily_search(
            query, max_results=max_results, search_depth=search_depth,
        )
    except RuntimeError as exc:
        return {"status": "error", "error": str(exc), "query": query}
    except Exception as exc:
        logger.exception("Tavily search failed for query=%s", query)
        return {"status": "error", "error": f"Search failed: {exc}", "query": query}

    # Increment quota (post-flight)
    if user_id:
        await increment_usage(db, user_id, "web_searches_daily", 1, plan_id)

    # Build card-shaped results and optionally save as Resources
    cards: list[dict[str, Any]] = []
    resource_keys: list[str] = []
    saved_any = False

    resource_repo = Repository(Resource, db, encryption) if save else None

    for r in raw_results:
        domain = urlparse(r.url).netloc if r.url else ""
        card: dict[str, Any] = {
            "name": r.title[:200] if r.title else r.url,
            "description": r.content[:500] if r.content else "",
            "url": r.url,
            "image": r.image_url,
            "tags": [t for t in ["web-search", domain] if t],
        }

        if save and resource_repo:
            resource = Resource(
                name=r.title[:200] if r.title else r.url[:200],
                uri=r.url,
                content=r.content,
                category="web_search",
                tags=["web-search", domain] if domain else ["web-search"],
                metadata={
                    "query": query,
                    "score": r.score,
                    "image_url": r.image_url,
                    "published_date": r.published_date,
                },
                user_id=user_id,
            )
            try:
                [saved_resource] = await resource_repo.upsert(resource)
                card["resource_id"] = str(saved_resource.id)
                resource_keys.append(saved_resource.name)
                saved_any = True
            except Exception:
                logger.exception("Failed to save resource for url=%s", r.url)

        cards.append(card)

    # Create a moment recording the search
    moment_id: str | None = None
    if user_id:
        try:
            moment_repo = Repository(Moment, db, encryption)
            moment_name = f"web-search-{query[:40]}"
            moment = Moment(
                name=moment_name,
                moment_type="web_search",
                summary=f"Searched for '{query}' — found {len(raw_results)} results",
                topic_tags=["web-search"],
                metadata={
                    "query": query,
                    "result_count": len(raw_results),
                    "resource_keys": resource_keys,
                    "search_depth": search_depth,
                },
                user_id=user_id,
                source_session_id=session_id,
            )
            [saved_moment] = await moment_repo.upsert(moment)
            moment_id = str(saved_moment.id)
        except Exception:
            logger.exception("Failed to save web_search moment")

    return {
        "status": "ok",
        "query": query,
        "results": cards,
        "count": len(cards),
        "moment_id": moment_id,
        "saved": saved_any,
    }
