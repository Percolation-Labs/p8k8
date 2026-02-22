"""Tavily web search — thin async wrapper around api.tavily.com/search."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from p8.settings import get_settings

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class SearchResult:
    title: str
    url: str
    content: str
    score: float
    image_url: str | None = None
    published_date: str | None = None


async def search(
    query: str,
    *,
    max_results: int = 5,
    search_depth: str = "basic",
    include_images: bool = True,
) -> list[SearchResult]:
    """Search the web via Tavily REST API.

    Args:
        query: Search query string.
        max_results: Number of results (1-20).
        search_depth: "basic" (faster/cheaper) or "advanced".
        include_images: Whether to request image URLs.

    Returns:
        List of SearchResult dataclass instances.

    Raises:
        httpx.HTTPStatusError: On non-2xx responses.
        RuntimeError: If tavily_api_key is not configured.
    """
    settings = get_settings()
    if not settings.tavily_api_key:
        raise RuntimeError("P8_TAVILY_API_KEY is not configured")

    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_images": include_images,
        "include_image_descriptions": False,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _TAVILY_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.tavily_api_key}",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    # Build image lookup from top-level images array (url → image_url)
    images: dict[str, str] = {}
    for img in data.get("images", []) or []:
        if isinstance(img, dict):
            images[img.get("url", "")] = img.get("url", "")
        elif isinstance(img, str):
            images[img] = img

    results = []
    for item in data.get("results", []):
        results.append(SearchResult(
            title=item.get("title", ""),
            url=item.get("url", ""),
            content=item.get("content", ""),
            score=item.get("score", 0.0),
            image_url=images.get(item.get("url", "")),
            published_date=item.get("published_date"),
        ))

    return results
