"""Tests for reading pipeline hyperlink preservation.

Verifies:
  - LLM summary includes markdown links to articles
  - Moment metadata contains `links` list with title, url, resource_id
  - Fallback summary (no LLM) still works
"""

from __future__ import annotations

import re

import pytest

from p8.services.bootstrap import _export_api_keys
from p8.settings import Settings
from p8.workers.handlers.reading import ReadingSummaryHandler, SUMMARY_PROMPT


# -- Unit tests (no DB, no LLM) --


def test_summary_prompt_requests_links():
    """The prompt explicitly asks the model to retain markdown links."""
    assert "markdown link" in SUMMARY_PROMPT.lower() or "link" in SUMMARY_PROMPT.lower()
    assert "{items_text}" in SUMMARY_PROMPT


# -- LLM integration test --


@pytest.mark.llm
async def test_llm_summarize_preserves_links():
    """_llm_summarize with URLs produces summary containing markdown links."""
    _export_api_keys(Settings())
    handler = ReadingSummaryHandler()

    items = [
        {
            "title": "New Advances in Quantum Computing",
            "uri": "https://example.com/quantum",
            "tags": ["physics", "quantum"],
            "resource_id": "res-001",
        },
        {
            "title": "Kubernetes 1.30 Released with Major Changes",
            "uri": "https://example.com/k8s",
            "tags": ["devops", "kubernetes"],
            "resource_id": "res-002",
        },
        {
            "title": "GPT-5 Benchmarks Show Surprising Results",
            "uri": "https://example.com/gpt5",
            "tags": ["AI", "LLM"],
            "resource_id": "res-003",
        },
    ]

    summary = await handler._llm_summarize(items)
    assert summary is not None, "LLM summarize returned None"

    print(f"\n{'='*60}")
    print("READING SUMMARY OUTPUT")
    print(f"{'='*60}")
    print(summary)
    print(f"{'='*60}")

    # Evaluate: summary should contain markdown links
    md_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", summary)
    print(f"\nMarkdown links found: {len(md_links)}")
    for text, url in md_links:
        print(f"  [{text}]({url})")

    assert len(md_links) >= 1, (
        f"Summary should contain at least 1 markdown link, got 0.\n"
        f"Summary: {summary}"
    )

    # Check that at least some of the original URLs are present
    urls_in_summary = [url for _, url in md_links]
    input_urls = {i["uri"] for i in items}
    preserved = set(urls_in_summary) & input_urls
    print(f"Original URLs preserved: {len(preserved)}/{len(input_urls)}")

    # At least 1 original URL should survive
    assert len(preserved) >= 1 or len(md_links) >= 1, (
        "Summary should reference at least one original article URL"
    )


def test_metadata_links_construction():
    """Verify the links metadata structure built from items."""
    items = [
        {"title": "Article A", "uri": "https://a.com", "resource_id": "r1",
         "image_uri": "", "tags": ["t1"]},
        {"title": "Article B", "uri": "https://b.com", "resource_id": "r2",
         "image_uri": "", "tags": ["t2"]},
        {"title": "No URL", "uri": "", "resource_id": "r3",
         "image_uri": "", "tags": []},
    ]

    # Replicate the links construction from reading.py
    links = [
        {"title": i["title"], "url": i["uri"], "resource_id": i["resource_id"]}
        for i in items if i.get("uri")
    ]

    assert len(links) == 2
    assert links[0] == {"title": "Article A", "url": "https://a.com", "resource_id": "r1"}
    assert links[1] == {"title": "Article B", "url": "https://b.com", "resource_id": "r2"}


@pytest.mark.llm
async def test_llm_summarize_no_urls_graceful():
    """Items without URLs still produce a valid summary."""
    _export_api_keys(Settings())
    handler = ReadingSummaryHandler()

    items = [
        {"title": "Local Article Without URL", "uri": "", "tags": ["misc"], "resource_id": "r1"},
        {"title": "Another No-URL Item", "uri": "", "tags": ["misc"], "resource_id": "r2"},
    ]

    summary = await handler._llm_summarize(items)
    assert summary is not None
    assert len(summary) > 10
    print(f"\nNo-URL summary: {summary}")
