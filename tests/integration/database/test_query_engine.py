"""Tests for REM dialect parser, query engine, and prompt builder."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import pytest_asyncio

from p8.services.database import Database
from p8.services.database.query_engine import RemQuery, RemQueryEngine, RemQueryParser
from p8.services.database.rem_prompt import REM_GRAMMAR, build_rem_prompt
from p8.settings import Settings


# ============================================================================
# Parser tests
# ============================================================================


class TestRemQueryParserLookup:
    def setup_method(self):
        self.parser = RemQueryParser()

    def test_single_key(self):
        q = self.parser.parse('LOOKUP "sarah-chen"')
        assert q.mode == "LOOKUP"
        assert q.params == {"key": "sarah-chen"}

    def test_single_key_unquoted(self):
        q = self.parser.parse("LOOKUP sarah-chen")
        assert q.mode == "LOOKUP"
        assert q.params == {"key": "sarah-chen"}

    def test_multi_key_comma(self):
        q = self.parser.parse('LOOKUP "sarah-chen", "project-atlas"')
        assert q.mode == "LOOKUP"
        assert q.params == {"keys": ["sarah-chen", "project-atlas"]}

    def test_case_insensitive_keyword(self):
        q = self.parser.parse('lookup "test"')
        assert q.mode == "LOOKUP"

    def test_missing_key_raises(self):
        with pytest.raises(ValueError, match="requires at least one key"):
            self.parser.parse("LOOKUP")


class TestRemQueryParserSearch:
    def setup_method(self):
        self.parser = RemQueryParser()

    def test_basic(self):
        q = self.parser.parse('SEARCH "database migration"')
        assert q.mode == "SEARCH"
        assert q.params["query_text"] == "database migration"

    def test_with_from_and_limit(self):
        q = self.parser.parse('SEARCH "database migration" FROM ontologies LIMIT 5')
        assert q.mode == "SEARCH"
        assert q.params["query_text"] == "database migration"
        assert q.params["table"] == "ontologies"
        assert q.params["limit"] == 5

    def test_with_field_and_min_similarity(self):
        q = self.parser.parse(
            'SEARCH "auth" FROM schemas FIELD content MIN_SIMILARITY 0.6'
        )
        assert q.params["field"] == "content"
        assert q.params["min_similarity"] == 0.6

    def test_kwarg_style(self):
        q = self.parser.parse('SEARCH "topic" table=resources limit=5')
        assert q.params["table"] == "resources"
        assert q.params["limit"] == 5

    def test_missing_query_raises(self):
        with pytest.raises(ValueError, match="requires a positional argument"):
            self.parser.parse("SEARCH FROM ontologies")


class TestRemQueryParserFuzzy:
    def setup_method(self):
        self.parser = RemQueryParser()

    def test_basic(self):
        q = self.parser.parse('FUZZY "sara chen"')
        assert q.mode == "FUZZY"
        assert q.params["query_text"] == "sara chen"

    def test_with_threshold_and_limit(self):
        q = self.parser.parse('FUZZY "projct atls" THRESHOLD 0.2 LIMIT 10')
        assert q.params["threshold"] == 0.2
        assert q.params["limit"] == 10


class TestRemQueryParserTraverse:
    def setup_method(self):
        self.parser = RemQueryParser()

    def test_basic(self):
        q = self.parser.parse('TRAVERSE "sarah-chen"')
        assert q.mode == "TRAVERSE"
        assert q.params["start_key"] == "sarah-chen"

    def test_with_depth_and_type(self):
        q = self.parser.parse('TRAVERSE "project-atlas" DEPTH 2 TYPE member')
        assert q.params["max_depth"] == 2
        assert q.params["rel_type"] == "member"

    def test_load_flag(self):
        q = self.parser.parse('TRAVERSE "overview" DEPTH 2 LOAD')
        assert q.params["start_key"] == "overview"
        assert q.params["max_depth"] == 2
        assert q.params["load"] is True

    def test_load_flag_default_absent(self):
        q = self.parser.parse('TRAVERSE "overview"')
        assert "load" not in q.params


class TestRemQueryParserSQL:
    def setup_method(self):
        self.parser = RemQueryParser()

    def test_explicit_sql_keyword(self):
        q = self.parser.parse("SQL SELECT name FROM schemas LIMIT 5")
        assert q.mode == "SQL"
        assert q.params["sql"] == "SELECT name FROM schemas LIMIT 5"

    def test_implicit_sql_fallback(self):
        q = self.parser.parse("SELECT name FROM schemas LIMIT 5")
        assert q.mode == "SQL"
        assert q.params["sql"] == "SELECT name FROM schemas LIMIT 5"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Empty query"):
            self.parser.parse("")

    def test_unmatched_quotes_fallback(self):
        q = self.parser.parse('SELECT "unclosed')
        assert q.mode == "SQL"


# ============================================================================
# Engine dispatch tests (mocked Database)
# ============================================================================


class TestRemQueryEngineDispatch:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.rem_lookup = AsyncMock(return_value=[{"entity_type": "user", "data": {}}])
        db.rem_fuzzy = AsyncMock(return_value=[{"entity_type": "user", "data": {}}])
        db.rem_search = AsyncMock(return_value=[{"entity_type": "schema", "data": {}}])
        db.rem_traverse = AsyncMock(return_value=[{"entity_type": "user", "data": {}}])
        db.fetch = AsyncMock(return_value=[])
        return db

    @pytest.fixture
    def mock_provider(self):
        provider = MagicMock()
        provider.embed = AsyncMock(return_value=[[0.1] * 1536])
        return provider

    @pytest.fixture
    def engine(self, mock_db):
        return RemQueryEngine(mock_db, Settings(), _embedding_provider=MagicMock())

    @pytest.mark.asyncio
    async def test_lookup_dispatch(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        result = await engine.execute('LOOKUP "sarah-chen"')
        mock_db.rem_lookup.assert_called_once_with(
            "sarah-chen", tenant_id=None, user_id=None
        )
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_dispatch(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        await engine.execute('FUZZY "sara" LIMIT 5')
        mock_db.rem_fuzzy.assert_called_once_with(
            "sara", tenant_id=None, user_id=None, threshold=0.3, limit=5
        )

    @pytest.mark.asyncio
    async def test_search_auto_embeds(self, mock_db, mock_provider):
        engine = RemQueryEngine(mock_db, Settings(), _embedding_provider=mock_provider)
        await engine.execute('SEARCH "database" FROM schemas')
        mock_provider.embed.assert_called_once_with(["database"])
        mock_db.rem_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_uses_settings_provider(self, mock_db):
        """SEARCH auto-creates a provider from settings (local by default)."""
        engine = RemQueryEngine(mock_db, Settings())
        await engine.execute('SEARCH "database" FROM schemas')
        mock_db.rem_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_traverse_dispatch(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        await engine.execute('TRAVERSE "sarah-chen" DEPTH 2 TYPE member')
        mock_db.rem_traverse.assert_called_once_with(
            "sarah-chen", tenant_id=None, user_id=None, max_depth=2, rel_type="member", load=False
        )

    @pytest.mark.asyncio
    async def test_sql_dispatch(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        await engine.execute("SQL SELECT name FROM schemas LIMIT 5")
        mock_db.fetch.assert_called_once_with("SELECT name FROM schemas LIMIT 5")

    @pytest.mark.asyncio
    async def test_tenant_id_passed_through(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        test_uid = UUID("00000000-0000-0000-0000-000000000001")
        await engine.execute('LOOKUP "key"', tenant_id="acme", user_id=test_uid)
        mock_db.rem_lookup.assert_called_once_with(
            "key", tenant_id="acme", user_id=test_uid
        )

    @pytest.mark.asyncio
    async def test_multi_key_lookup(self, mock_db):
        engine = RemQueryEngine(mock_db, Settings())
        await engine.execute('LOOKUP "a", "b"')
        assert mock_db.rem_lookup.call_count == 2


# ============================================================================
# SQL safety tests
# ============================================================================


class TestSQLSafety:
    @pytest.fixture
    def engine(self):
        db = MagicMock()
        db.fetch = AsyncMock(return_value=[])
        return RemQueryEngine(db, Settings())

    @pytest.mark.asyncio
    async def test_drop_blocked(self, engine):
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await engine.execute("SQL DROP TABLE schemas")

    @pytest.mark.asyncio
    async def test_truncate_blocked(self, engine):
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await engine.execute("SQL TRUNCATE schemas")

    @pytest.mark.asyncio
    async def test_alter_blocked(self, engine):
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await engine.execute("SQL ALTER TABLE schemas ADD COLUMN foo TEXT")

    @pytest.mark.asyncio
    async def test_bare_delete_blocked(self, engine):
        with pytest.raises(ValueError, match="DELETE without WHERE"):
            await engine.execute("SQL DELETE FROM schemas")

    @pytest.mark.asyncio
    async def test_delete_with_where_allowed(self, engine):
        await engine.execute("SQL DELETE FROM schemas WHERE id = '123'")
        engine.db.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_allowed(self, engine):
        await engine.execute("SQL SELECT * FROM schemas")
        engine.db.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_blocked(self, engine):
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await engine.execute("SQL CREATE TABLE foo (id INT)")

    @pytest.mark.asyncio
    async def test_grant_blocked(self, engine):
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await engine.execute("SQL GRANT ALL ON schemas TO public")


# ============================================================================
# Prompt tests
# ============================================================================


class TestRemPrompt:
    def test_grammar_contains_bnf(self):
        assert "LOOKUP" in REM_GRAMMAR
        assert "SEARCH" in REM_GRAMMAR
        assert "FUZZY" in REM_GRAMMAR
        assert "TRAVERSE" in REM_GRAMMAR
        assert "<query>" in REM_GRAMMAR

    @pytest.mark.asyncio
    async def test_build_rem_prompt_with_tables(self):
        mock_db = MagicMock()
        mock_db.fetch = AsyncMock(
            return_value=[
                {
                    "name": "ontologies",
                    "description": "Domain knowledge entities",
                    "json_schema": {
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                            "uri": {"type": "string"},
                        }
                    },
                },
                {
                    "name": "resources",
                    "description": "Documents and artifacts",
                    "json_schema": {
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                        }
                    },
                },
            ]
        )
        prompt = await build_rem_prompt(mock_db)
        assert "ontologies" in prompt
        assert "resources" in prompt
        assert "Domain knowledge entities" in prompt
        assert "content, name, uri" in prompt  # sorted

    @pytest.mark.asyncio
    async def test_build_rem_prompt_no_tables(self):
        mock_db = MagicMock()
        mock_db.fetch = AsyncMock(return_value=[])
        prompt = await build_rem_prompt(mock_db)
        assert "No table schemas registered" in prompt

    @pytest.mark.asyncio
    async def test_prompt_includes_grammar(self):
        mock_db = MagicMock()
        mock_db.fetch = AsyncMock(return_value=[])
        prompt = await build_rem_prompt(mock_db)
        assert "BNF Grammar" in prompt
        assert "LOOKUP" in prompt


# ============================================================================
# Integration tests (require live DB)
# ============================================================================


@pytest.mark.asyncio
class TestQueryEngineIntegration:
    @pytest.fixture(autouse=True)
    async def _clean(self, clean_db):
        pass

    async def test_lookup_roundtrip(self, db):
        """Insert a schema, then LOOKUP by normalized key."""
        from tests.conftest import det_id
        sid = det_id("schemas", "query-test-agent")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
            " VALUES ($1, 'query-test-agent', 'agent',"
            " 'A test agent', 'test content', '{}'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid,
        )
        results = await db.rem_query('LOOKUP "query-test-agent"')
        assert len(results) >= 1
        # rem_lookup returns full entity row: {id, name, kind, ...}
        assert any(r["data"]["name"] == "query-test-agent" for r in results)

    async def test_fuzzy_roundtrip(self, db):
        """Insert a schema, then FUZZY search for it."""
        from tests.conftest import det_id
        sid = det_id("schemas", "fuzzy-target-entity")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
            " VALUES ($1, 'fuzzy-target-entity', 'model',"
            " 'A fuzzy test', 'fuzzy content', '{}'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid,
        )
        results = await db.rem_query('FUZZY "fuzzy target" LIMIT 5')
        # Fuzzy should find it via trigram matching on kv_store
        assert isinstance(results, list)

    async def test_sql_roundtrip(self, db):
        """SQL mode executes raw queries."""
        results = await db.rem_query("SQL SELECT name FROM schemas LIMIT 5")
        assert isinstance(results, list)

    async def test_sql_blocked_keyword(self, db):
        """SQL mode rejects dangerous statements."""
        with pytest.raises(ValueError, match="Blocked SQL keyword"):
            await db.rem_query("SQL DROP TABLE schemas")

    async def test_traverse_roundtrip(self, db):
        """Insert entities with graph_edges, then traverse."""
        from tests.conftest import det_id
        parent_id = det_id("schemas", "traverse-parent")
        child_id = det_id("schemas", "traverse-child")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema,"
            " graph_edges)"
            " VALUES ($1, 'traverse-parent', 'model',"
            " 'parent', 'parent content', '{}'::jsonb,"
            " '[{\"target\": \"traverse-child\", \"rel_type\": \"has_child\", \"weight\": 1.0}]'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET graph_edges = EXCLUDED.graph_edges",
            parent_id,
        )
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content, json_schema)"
            " VALUES ($1, 'traverse-child', 'model',"
            " 'child', 'child content', '{}'::jsonb)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            child_id,
        )
        results = await db.rem_query('TRAVERSE "traverse-parent" DEPTH 1')
        assert isinstance(results, list)

    async def test_implicit_sql_fallback(self, db):
        """Strings not starting with a mode keyword fall back to SQL."""
        results = await db.rem_query("SELECT count(*) FROM schemas")
        assert isinstance(results, list)
