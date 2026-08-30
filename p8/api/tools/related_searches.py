"""related_searches tool — "People also search for"-style related phrases."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def related_searches(query: str, max_results: int = 8) -> dict[str, Any]:
    """Find related search phrases for a query ("People also search for").

    Calls Google's public autosuggest endpoint — no API key required. See
    docs/related-searches.md for why this endpoint is used instead of
    scraping the Google SERP HTML directly.

    Args:
        query: What to find related searches for.
        max_results: Maximum number of related phrases (default 8).

    Returns:
        Dict with status, query, results (list of phrases), count.
    """
    from p8.services.related_searches import related_searches as fetch_related

    try:
        phrases = await fetch_related(query, max_results=max_results)
    except Exception as exc:
        logger.exception("related_searches failed for query=%s", query)
        return {"status": "error", "error": f"Lookup failed: {exc}", "query": query}

    return {
        "status": "ok",
        "query": query,
        "results": phrases,
        "count": len(phrases),
    }
