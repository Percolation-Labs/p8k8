"""Tests for api/tools/ — search, action, ask_agent + user resource."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from p8.api.tools import init_tools
from p8.api.tools.action import action

USER_ADA = UUID("00000000-0000-0000-0000-00000000ada0")
from p8.api.tools.search import search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _setup_tools(db, encryption, clean_db):
    """Initialize tool module state with live DB + encryption."""
    init_tools(db, encryption)


@pytest.fixture
async def _seed_schema(db):
    """Insert a schema row for search tests."""
    from tests.conftest import det_id
    sid = det_id("schemas", "search-tool-test")
    await db.execute(
        "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
        " VALUES ($1, 'search-tool-test', 'agent',"
        " 'A test agent for tool tests', 'test content', '{}'::jsonb)"
        " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
        sid,
    )


# ---------------------------------------------------------------------------
# search tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_lookup(_seed_schema):
    result = await search(query='LOOKUP "search-tool-test"')
    assert result["status"] == "success"
    assert result["count"] >= 1
    assert any(
        r["data"]["name"] == "search-tool-test" for r in result["results"]
    )


@pytest.mark.asyncio
async def test_search_fuzzy(_seed_schema):
    result = await search(query='FUZZY "search tool" LIMIT 5')
    assert result["status"] == "success"
    assert isinstance(result["results"], list)


@pytest.mark.asyncio
async def test_search_sql():
    result = await search(query="SQL SELECT name, kind FROM schemas LIMIT 3")
    assert result["status"] == "success"
    assert isinstance(result["results"], list)


@pytest.mark.asyncio
async def test_search_error_returns_status():
    result = await search(query="SQL DROP TABLE schemas")
    assert result["status"] == "error"
    assert "Blocked" in result["error"]


# ---------------------------------------------------------------------------
# action tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_observation():
    result = await action(type="observation", payload={"confidence": 0.95})
    assert result["status"] == "success"
    assert result["_action_event"] is True
    assert result["action_type"] == "observation"
    assert result["payload"]["confidence"] == 0.95


@pytest.mark.asyncio
async def test_action_no_payload():
    result = await action(type="elicit")
    assert result["status"] == "success"
    assert "payload" not in result


# ---------------------------------------------------------------------------
# user resource (MCP resource callable)
# ---------------------------------------------------------------------------


@pytest.fixture
async def _seed_user(db, encryption):
    """Insert a user row for resource tests."""
    from p8.ontology.types import User
    from p8.services.repository import Repository

    repo = Repository(User, db, encryption)
    user = User(
        name="Ada Lovelace",
        email="ada@example.com",
        content="Mathematician and first programmer.",
        metadata={"role": "admin"},
        tags=["engineering", "math"],
        user_id=USER_ADA,
    )
    await repo.upsert(user)


@pytest.mark.asyncio
async def test_user_resource_found(_seed_user):
    from p8.api.mcp_server import user_profile

    result_str = await user_profile(str(USER_ADA))
    data = json.loads(result_str)
    assert data["name"] == "Ada Lovelace"
    assert data["email"] == "ada@example.com"
    assert data["content"] == "Mathematician and first programmer."
    assert data["metadata"] == {"role": "admin"}
    assert "engineering" in data["tags"]
    # Should NOT include extra fields like id, created_at, etc.
    assert "id" not in data
    assert "created_at" not in data


@pytest.mark.asyncio
async def test_user_resource_not_found():
    from p8.api.mcp_server import user_profile

    result_str = await user_profile("nonexistent")
    data = json.loads(result_str)
    assert "error" in data
