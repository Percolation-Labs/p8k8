"""Integration tests for CLI — require a running Postgres."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.conftest import det_id


@pytest_asyncio.fixture(autouse=True)
async def _clean(clean_db):
    yield


class TestQueryIntegration:
    """Integration tests — require a running postgres with p8 schema."""

    @pytest.mark.asyncio
    async def test_query_roundtrip(self, db):
        """Insert data, then verify rem_query works (used by CLI)."""
        sid = det_id("schemas", "cli-test-entity")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
            " VALUES ($1, 'cli-test-entity', 'model',"
            " 'A CLI test', 'test content', '{}'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid,
        )
        results = await db.rem_query('LOOKUP "cli-test-entity"')
        assert len(results) >= 1
        assert any(r["data"]["name"] == "cli-test-entity" for r in results)

    @pytest.mark.asyncio
    async def test_fuzzy_roundtrip(self, db):
        sid = det_id("schemas", "cli-fuzzy-test")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
            " VALUES ($1, 'cli-fuzzy-test', 'model',"
            " 'A fuzzy test', 'fuzzy content', '{}'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid,
        )
        results = await db.rem_query('FUZZY "cli fuzzy" LIMIT 5')
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_sql_roundtrip(self, db):
        results = await db.rem_query("SELECT name FROM schemas LIMIT 3")
        assert isinstance(results, list)
