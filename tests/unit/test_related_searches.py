"""Unit tests for the related_searches service and tool.

Mocks the outbound HTTP call to Google's autosuggest endpoint — no network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from p8.api.tools.related_searches import related_searches as related_searches_tool
from p8.services.related_searches import related_searches


def _mock_response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


async def test_related_searches_extracts_phrases():
    payload = ["cats", ["cats vs dogs", "cat breeds", "cat food"]]
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        results = await related_searches("cats")

    assert results == ["cats vs dogs", "cat breeds", "cat food"]


async def test_related_searches_dedupes_case_insensitive():
    payload = ["cats", ["Cat Breeds", "cat breeds", "cats", "Cat Food"]]
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        results = await related_searches("cats")

    # Original query dropped, case-insensitive dupe of "cat breeds" dropped.
    assert results == ["Cat Breeds", "Cat Food"]


async def test_related_searches_caps_at_max_results():
    payload = ["x", [f"phrase {i}" for i in range(20)]]
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        results = await related_searches("x", max_results=3)

    assert len(results) == 3
    assert results == ["phrase 0", "phrase 1", "phrase 2"]


async def test_related_searches_handles_empty_response():
    payload = ["nonsense", []]
    with patch("httpx.AsyncClient") as mock_client_cls:
        client = mock_client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(payload))

        results = await related_searches("nonsense")

    assert results == []


async def test_tool_returns_ok_shape():
    with patch(
        "p8.services.related_searches.related_searches",
        new=AsyncMock(return_value=["a", "b"]),
    ):
        result = await related_searches_tool("query")

    assert result == {
        "status": "ok",
        "query": "query",
        "results": ["a", "b"],
        "count": 2,
    }


async def test_tool_returns_error_on_exception():
    with patch(
        "p8.services.related_searches.related_searches",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await related_searches_tool("query")

    assert result["status"] == "error"
    assert result["query"] == "query"
    assert "boom" in result["error"]
