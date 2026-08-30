"""Unit tests for the Percolate source clients (§15.1) — mocked HTTP, no network.

Payload shapes mirror real live responses captured from each API during
development (SEC EDGAR full-text search, arXiv Atom feed, HN Firebase API,
Federal Register, Wikipedia RecentChanges).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from p8.services.percolate.sources import (
    fetch_arxiv,
    fetch_federal_register,
    fetch_hn,
    fetch_sec_edgar,
    fetch_wikipedia_recent_changes,
)


def _mock_response(payload=None, text=None):
    resp = MagicMock()
    if payload is not None:
        resp.json.return_value = payload
    if text is not None:
        resp.text = text
    resp.raise_for_status = MagicMock()
    resp.status_code = 200
    return resp


async def test_fetch_sec_edgar_normalizes_hits():
    payload = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "ciks": ["0002098430"],
                        "display_names": ["Madison Air Solutions Corp  (MAIR)  (CIK 0002098430)"],
                        "form": "8-K",
                        "root_forms": ["8-K"],
                        "file_date": "2026-08-25",
                        "adsh": "0001628280-26-058764",
                        "items": ["1.01", "7.01"],
                    }
                }
            ]
        }
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        items = await fetch_sec_edgar(query="acquisition", user_agent="test-agent test@example.com")

    assert len(items) == 1
    item = items[0]
    assert item["source_key"] == "sec_edgar"
    assert item["entity_names"] == ["Madison Air Solutions Corp"]
    assert "0001628280-26-058764" in item["url"]
    assert item["published_at"].year == 2026


async def test_fetch_arxiv_parses_atom_entries():
    body = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2608.27454v1</id>
    <title>WikiSkill: Compiling Agent Experience</title>
    <published>2026-08-27T17:59:11Z</published>
    <updated>2026-08-27T17:59:11Z</updated>
    <summary>Agent skills package specialized knowledge.</summary>
    <link href="https://arxiv.org/abs/2608.27454v1" rel="alternate" type="text/html"/>
    <author><name>Liyan Tang</name></author>
    <author><name>Cyrus Rashtchian</name></author>
    <arxiv:primary_category term="cs.AI"/>
  </entry>
</feed>"""
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(text=body))

        items = await fetch_arxiv(query="cat:cs.AI", limit=5)

    assert len(items) == 1
    item = items[0]
    assert item["source_key"] == "arxiv"
    assert item["title"].startswith("WikiSkill")
    assert item["entity_names"] == ["Liyan Tang", "Cyrus Rashtchian"]
    assert item["url"] == "https://arxiv.org/abs/2608.27454v1"
    assert item["published_at"].year == 2026


async def test_fetch_hn_extracts_domain_as_entity():
    topstories = [49496782, 49496918]
    item1 = {
        "by": "joebig", "descendants": 15, "id": 49496782, "score": 67, "type": "story",
        "time": 1788078206, "title": "Longest Straight Line Paths", "url": "https://arxiv.org/abs/1804.07389",
    }
    item2 = {
        "by": "someone", "id": 49496918, "score": 10, "type": "story",
        "time": 1788078300, "title": "Ask HN: no url here",
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[
            _mock_response(topstories), _mock_response(item1), _mock_response(item2),
        ])

        items = await fetch_hn(limit=2)

    assert len(items) == 2
    assert items[0]["entity_names"] == ["arxiv.org"]
    assert items[1]["entity_names"] == []
    assert items[1]["url"] == "https://news.ycombinator.com/item?id=49496918"


async def test_fetch_federal_register_uses_agencies_as_entities():
    payload = {
        "results": [
            {
                "title": "Further Ensuring Affordable Beef",
                "abstract": "A presidential document.",
                "html_url": "https://www.federalregister.gov/documents/2026/08/31/x",
                "publication_date": "2026-08-31",
                "agencies": [{"name": "Executive Office of the President"}],
            }
        ]
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        items = await fetch_federal_register(limit=5)

    assert len(items) == 1
    assert items[0]["entity_names"] == ["Executive Office of the President"]
    assert items[0]["source_key"] == "federal_register"


async def test_fetch_wikipedia_uses_page_title_as_entity():
    payload = {
        "query": {
            "recentchanges": [
                {"type": "edit", "title": "Henley Festival", "timestamp": "2026-08-30T11:28:57Z"},
            ]
        }
    }
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        items = await fetch_wikipedia_recent_changes(limit=5, user_agent="test-agent test@example.com")

    assert len(items) == 1
    assert items[0]["entity_names"] == ["Henley Festival"]
    assert items[0]["url"] == "https://en.wikipedia.org/wiki/Henley_Festival"
