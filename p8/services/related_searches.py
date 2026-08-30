"""Related searches ("People also search for") — thin async wrapper around
Google's public autosuggest endpoint. No API key required.

See docs/related-searches.md for why this endpoint was chosen over scraping
the Google SERP HTML directly.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_SUGGEST_URL = "https://suggestqueries.google.com/complete/search"


async def related_searches(query: str, *, max_results: int = 8) -> list[str]:
    """Fetch related search phrases for a query.

    Args:
        query: The search term to find related phrases for.
        max_results: Maximum number of phrases to return (default 8).

    Returns:
        Deduped list of related phrases, excluding the original query.

    Raises:
        httpx.HTTPStatusError: On non-2xx responses.
    """
    params = {"client": "firefox", "q": query}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(_SUGGEST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    # Response shape: [query, [phrase, phrase, ...]]
    raw_phrases = data[1] if len(data) > 1 and isinstance(data[1], list) else []

    seen: set[str] = {query.strip().lower()}
    results: list[str] = []
    for phrase in raw_phrases:
        if not isinstance(phrase, str):
            continue
        key = phrase.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(phrase)
        if len(results) >= max_results:
            break

    return results
