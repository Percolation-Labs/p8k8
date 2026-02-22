"""Tests for KV store triggers and indexes.

Verifies that:
1. INSERT/UPDATE/DELETE on entity tables auto-sync to kv_store via triggers
2. KV store indexes support fast lookups (trigram, type, graph)
3. rebuild_kv_store() and rebuild_kv_store_incremental() work correctly
4. normalize_key() produces expected kebab-case keys
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.conftest import det_id


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# normalize_key tests
# ---------------------------------------------------------------------------


class TestNormalizeKey:
    async def test_basic(self, db):
        assert await db.fetchval("SELECT normalize_key('Hello World')") == "hello-world"

    async def test_underscores(self, db):
        assert await db.fetchval("SELECT normalize_key('my_entity_name')") == "my-entity-name"

    async def test_special_chars(self, db):
        assert await db.fetchval("SELECT normalize_key('Test (v2.0) — final!')") == "test-v20-final"

    async def test_multiple_spaces(self, db):
        assert await db.fetchval("SELECT normalize_key('  hello   world  ')") == "hello-world"

    async def test_already_kebab(self, db):
        assert await db.fetchval("SELECT normalize_key('already-kebab-case')") == "already-kebab-case"


# ---------------------------------------------------------------------------
# KV trigger tests — INSERT
# ---------------------------------------------------------------------------


class TestKVInsertTrigger:
    async def test_schema_insert_syncs_to_kv(self, db):
        sid = det_id("schemas", "kv-insert-test")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description, content)"
            " VALUES ($1, $2, $3, $4, $5)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description, content = EXCLUDED.content",
            sid, "kv-insert-test", "agent", "A test schema", "Full content here",
        )
        row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id=$1", sid)
        assert row is not None
        assert row["entity_key"] == "kv-insert-test"
        assert row["entity_type"] == "schemas"
        # schemas kv_summary_expr = COALESCE(content, description, name)
        assert row["content_summary"] == "Full content here"

    async def test_ontology_uses_name_summary(self, db):
        """Encrypted table → KV summary is just the name."""
        oid = det_id("ontologies", "kv-onto-test")
        await db.execute(
            "INSERT INTO ontologies (id, name, content) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content",
            oid, "kv-onto-test", "Sensitive content should not appear",
        )
        row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id=$1", oid)
        assert row is not None
        assert row["content_summary"] == "kv-onto-test"

    async def test_server_uses_description(self, db):
        srv_id = det_id("servers", "kv-server-test")
        await db.execute(
            "INSERT INTO servers (id, name, description) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            srv_id, "kv-server-test", "A tool server",
        )
        row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id=$1", srv_id)
        assert row is not None
        assert row["content_summary"] == "A tool server"

    async def test_graph_edges_synced(self, db):
        sid = det_id("schemas", "kv-edges-test")
        edges = [{"target": "other-entity", "relation": "related_to", "weight": 0.9}]
        await db.execute(
            "INSERT INTO schemas (id, name, kind, graph_edges)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET graph_edges = EXCLUDED.graph_edges",
            sid, "kv-edges-test", "model", edges,
        )
        row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id=$1", sid)
        stored = row["graph_edges"]
        assert len(stored) == 1
        assert stored[0]["target"] == "other-entity"

    async def test_metadata_synced(self, db):
        sid = det_id("schemas", "kv-meta-test")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, metadata)"
            " VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET metadata = EXCLUDED.metadata",
            sid, "kv-meta-test", "model", {"priority": "high"},
        )
        row = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id=$1", sid)
        assert row["metadata"]["priority"] == "high"


# ---------------------------------------------------------------------------
# KV trigger tests — UPDATE
# ---------------------------------------------------------------------------


class TestKVUpdateTrigger:
    async def test_update_syncs_summary(self, db):
        sid = det_id("schemas", "kv-update")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, content) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content",
            sid, "kv-update", "model", "original",
        )
        assert (await db.fetchrow(
            "SELECT content_summary FROM kv_store WHERE entity_id=$1", sid
        ))["content_summary"] == "original"

        await db.execute("UPDATE schemas SET content='updated' WHERE id=$1", sid)
        assert (await db.fetchrow(
            "SELECT content_summary FROM kv_store WHERE entity_id=$1", sid
        ))["content_summary"] == "updated"

    async def test_soft_delete_removes(self, db):
        sid = det_id("schemas", "kv-softdel")
        await db.execute(
            "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET deleted_at = NULL",
            sid, "kv-softdel", "model",
        )
        assert await db.fetchval("SELECT COUNT(*) FROM kv_store WHERE entity_id=$1", sid) == 1
        await db.execute("UPDATE schemas SET deleted_at=CURRENT_TIMESTAMP WHERE id=$1", sid)
        assert await db.fetchval("SELECT COUNT(*) FROM kv_store WHERE entity_id=$1", sid) == 0


# ---------------------------------------------------------------------------
# KV trigger tests — DELETE
# ---------------------------------------------------------------------------


class TestKVDeleteTrigger:
    async def test_hard_delete_removes(self, db):
        sid = det_id("schemas", "kv-harddel")
        await db.execute(
            "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
            sid, "kv-harddel", "model",
        )
        assert await db.fetchval("SELECT COUNT(*) FROM kv_store WHERE entity_id=$1", sid) == 1
        await db.execute("DELETE FROM schemas WHERE id=$1", sid)
        assert await db.fetchval("SELECT COUNT(*) FROM kv_store WHERE entity_id=$1", sid) == 0


# ---------------------------------------------------------------------------
# KV index tests
# ---------------------------------------------------------------------------


class TestKVIndexes:
    async def test_trigram_indexes_exist(self, db):
        indexes = await db.fetch(
            "SELECT indexname FROM pg_indexes"
            " WHERE tablename='kv_store' AND indexname LIKE '%trgm%'"
        )
        names = {r["indexname"] for r in indexes}
        assert "idx_kv_store_key_trgm" in names
        assert "idx_kv_store_summary_trgm" in names

    async def test_type_index_exists(self, db):
        assert await db.fetchrow(
            "SELECT 1 FROM pg_indexes WHERE tablename='kv_store' AND indexname='idx_kv_store_type'"
        )

    async def test_graph_gin_index_exists(self, db):
        assert await db.fetchrow(
            "SELECT 1 FROM pg_indexes WHERE tablename='kv_store' AND indexname='idx_kv_store_graph'"
        )

    async def test_unique_tenant_key_index(self, db):
        assert await db.fetchrow(
            "SELECT 1 FROM pg_indexes WHERE tablename='kv_store' AND indexname='idx_kv_store_tenant_key'"
        )

    async def test_embedding_hnsw_indexes(self, db):
        rows = await db.fetch(
            "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_embeddings_%_hnsw'"
        )
        names = {r["indexname"] for r in rows}
        expected = {
            "idx_embeddings_schemas_hnsw", "idx_embeddings_ontologies_hnsw",
            "idx_embeddings_resources_hnsw", "idx_embeddings_moments_hnsw",
            "idx_embeddings_sessions_hnsw",
            "idx_embeddings_tools_hnsw", "idx_embeddings_users_hnsw",
            "idx_embeddings_files_hnsw",
        }
        assert expected.issubset(names)


# ---------------------------------------------------------------------------
# rebuild_kv_store tests
# ---------------------------------------------------------------------------


class TestRebuildKVStore:
    async def test_full_rebuild(self, db):
        sid = det_id("schemas", "rebuild-test")
        srv_id = det_id("servers", "rebuild-server")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid, "rebuild-test", "agent", "Test rebuild",
        )
        await db.execute(
            "INSERT INTO servers (id, name, description) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            srv_id, "rebuild-server", "A server",
        )

        # Delete KV entries for these specific entities, then rebuild
        await db.execute("DELETE FROM kv_store WHERE entity_id IN ($1, $2)", sid, srv_id)

        await db.execute("SELECT rebuild_kv_store()")
        assert await db.fetchrow("SELECT 1 FROM kv_store WHERE entity_id=$1", sid)
        assert await db.fetchrow("SELECT 1 FROM kv_store WHERE entity_id=$1", srv_id)

    async def test_incremental_rebuild(self, db):
        sid = det_id("schemas", "incr-test")
        await db.execute(
            "INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
            sid, "incr-test", "model", "Incremental",
        )

        await db.execute("DELETE FROM kv_store WHERE entity_id=$1", sid)
        assert await db.fetchval("SELECT COUNT(*) FROM kv_store WHERE entity_id=$1", sid) == 0

        repaired = await db.fetchval("SELECT rebuild_kv_store_incremental()")
        assert repaired >= 1
        assert await db.fetchrow("SELECT 1 FROM kv_store WHERE entity_id=$1", sid)


# ---------------------------------------------------------------------------
# REM function integration with KV
# ---------------------------------------------------------------------------


class TestREMWithKV:
    async def test_rem_lookup(self, db):
        sid = det_id("schemas", "Lookup Target")
        await db.execute(
            "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
            sid, "Lookup Target", "agent",
        )
        results = await db.rem_lookup("lookup-target")
        assert len(results) >= 1
        assert results[0]["data"]["id"] == str(sid)

    async def test_rem_fuzzy(self, db):
        sid = det_id("schemas", "fuzzy-kv-target")
        await db.execute(
            "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
            sid, "fuzzy-kv-target", "agent",
        )
        results = await db.rem_fuzzy("fuzzy kv target")
        assert any(r["data"]["id"] == str(sid) for r in results)

    async def test_rem_traverse(self, db):
        child_id = det_id("schemas", "traverse-child")
        parent_id = det_id("schemas", "traverse-parent")
        await db.execute(
            "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
            " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
            child_id, "traverse-child", "model",
        )
        edges = [{"target": "traverse-child", "relation": "depends_on", "weight": 1.0}]
        await db.execute(
            "INSERT INTO schemas (id, name, kind, graph_edges) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (id) DO UPDATE SET graph_edges = EXCLUDED.graph_edges",
            parent_id, "traverse-parent", "agent", edges,
        )

        results = await db.rem_traverse("traverse-parent", max_depth=1)
        keys = {r["entity_key"] for r in results}
        assert "traverse-parent" in keys
        assert "traverse-child" in keys
