"""Tests for embedding pipeline — upsert artifacts, queue triggers, auto-processing.

The core contract: any upsert into an entity table with has_embeddings=true
produces TWO artifacts automatically:
  1. KV store entry (via kv_store_upsert trigger) — immediate
  2. Embedding (via queue_embedding trigger → background worker) — within seconds

The EmbeddingWorker runs as a background task during tests, polling the queue
just like pg_cron would in production. Tests never call process_batch() directly.

IDs are deterministic hashes of entity names — no random UUIDs.
All writes use upsert-by-id (INSERT ... ON CONFLICT (id) DO UPDATE).
"""

from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID

import pytest
import pytest_asyncio

from p8.services.embeddings import EmbeddingService, EmbeddingWorker, LocalEmbeddingProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKER_POLL = 0.1  # 100ms — fast polling for tests


def det_id(name: str) -> UUID:
    """Deterministic UUID from a name. Same name → same ID, always."""
    return UUID(hashlib.md5(name.encode()).hexdigest())


async def wait_for_embedding(db, embeddings_table: str, entity_id: UUID, *, timeout: float = 5.0):
    """Poll until the embedding appears or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await db.fetchrow(
            f"SELECT * FROM {embeddings_table} WHERE entity_id = $1", entity_id,
        )
        if row is not None:
            return row
        await asyncio.sleep(WORKER_POLL)
    return None


async def wait_for_queue_clear(db, table: str, entity_id: UUID, *, timeout: float = 5.0):
    """Poll until the queue entry is gone."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await db.fetchrow(
            "SELECT 1 FROM embedding_queue WHERE table_name = $1 AND entity_id = $2",
            table, entity_id,
        )
        if row is None:
            return True
        await asyncio.sleep(WORKER_POLL)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def embedding_service(db, encryption, settings):
    provider = LocalEmbeddingProvider(dimensions=settings.embedding_dimensions)
    return EmbeddingService(db, provider, encryption, batch_size=settings.embedding_batch_size)


@pytest_asyncio.fixture
async def embedding_worker(db, embedding_service):
    """Start the background worker; stop it when the test finishes.

    Clears stale queue entries first so the worker only processes entries
    from the current test — prevents cross-test interference on timing.
    """
    await db.execute("DELETE FROM embedding_queue")
    worker = EmbeddingWorker(embedding_service, poll_interval=WORKER_POLL)
    task = asyncio.create_task(worker.run())
    yield worker
    await worker.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# End-to-end: one upsert → KV entry + embedding (auto-processed)
# ---------------------------------------------------------------------------

# Each case: (table, upsert_sql, name, content_value, embedding_field, embeddings_table, has_kv)
UPSERT_CASES = [
    pytest.param(
        "schemas",
        """INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)
           ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
        "e2e-schema", "An agent that queries the database",
        "description", "embeddings_schemas", True,
        id="schemas",
    ),
    pytest.param(
        "ontologies",
        """INSERT INTO ontologies (id, name, content) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content""",
        "e2e-ontology", "Domain knowledge about graph databases",
        "content", "embeddings_ontologies", True,
        id="ontologies",
    ),
    pytest.param(
        "resources",
        """INSERT INTO resources (id, name, content) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content""",
        "e2e-resource", "Chapter 3: API design patterns",
        "content", "embeddings_resources", True,
        id="resources",
    ),
    pytest.param(
        "moments",
        """INSERT INTO moments (id, name, summary) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET summary = EXCLUDED.summary""",
        "e2e-moment", "Team discussed migration strategy",
        "summary", "embeddings_moments", True,
        id="moments",
    ),
    pytest.param(
        "tools",
        """INSERT INTO tools (id, name, description) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
        "e2e-tool", "Searches the REM database by semantic similarity",
        "description", "embeddings_tools", True,
        id="tools",
    ),
    pytest.param(
        "users",
        """INSERT INTO users (id, name, content) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content""",
        "e2e-user", "Interested in distributed systems and Postgres",
        "content", "embeddings_users", True,
        id="users",
    ),
    pytest.param(
        "files",
        """INSERT INTO files (id, name, parsed_content) VALUES ($1, $2, $3)
           ON CONFLICT (id) DO UPDATE SET parsed_content = EXCLUDED.parsed_content""",
        "e2e-file", "Extracted text from the architecture PDF",
        "parsed_content", "embeddings_files", True,
        id="files",
    ),
]


class TestUpsertProducesArtifacts:
    """One upsert → both KV entry and embedding appear automatically."""

    @pytest.mark.parametrize(
        "table, upsert_sql, name, content_value, embedding_field, embeddings_table, has_kv",
        UPSERT_CASES,
    )
    async def test_insert_produces_kv_and_embedding(
        self, db, embedding_worker,
        table, upsert_sql, name, content_value, embedding_field, embeddings_table, has_kv,
    ):
        eid = det_id(name)

        # Clear stale embedding so worker always has fresh work
        await db.execute(f"DELETE FROM {embeddings_table} WHERE entity_id = $1", eid)
        await db.execute(
            "DELETE FROM embedding_queue WHERE table_name = $1 AND entity_id = $2",
            table, eid,
        )

        # Use a unique suffix so the trigger always fires (content changes each run)
        import time
        unique_content = f"{content_value} [{time.monotonic_ns()}]"

        # --- UPSERT ---
        if table == "schemas":
            await db.execute(upsert_sql, eid, name, "agent", unique_content)
        else:
            await db.execute(upsert_sql, eid, name, unique_content)

        # --- 1. KV store populated immediately (trigger is synchronous) ---
        if has_kv:
            kv = await db.fetchrow(
                "SELECT * FROM kv_store WHERE entity_id = $1 AND entity_type = $2",
                eid, table,
            )
            assert kv is not None, f"KV missing for {table}/{eid}"
            assert kv["entity_key"] is not None

        # --- 2. Embedding appears automatically (worker processes the queue) ---
        emb = await wait_for_embedding(db, embeddings_table, eid)
        assert emb is not None, f"Embedding not auto-produced for {table}/{eid}"
        assert emb["field_name"] == embedding_field
        assert emb["provider"] is not None  # "local" in tests, "openai" if cron races
        assert emb["content_hash"] is not None

        # --- 3. Queue cleared automatically ---
        cleared = await wait_for_queue_clear(db, table, eid)
        assert cleared, f"Queue not auto-cleared for {table}/{eid}"

    async def test_update_refreshes_both_artifacts(self, db, embedding_worker):
        """UPDATE the embedded field → KV summary updated + new embedding generated."""
        eid = det_id("update-e2e-schema")
        # Clear stale state from prior runs
        await db.execute("DELETE FROM embeddings_schemas WHERE entity_id = $1", eid)
        await db.execute(
            "DELETE FROM embedding_queue WHERE table_name = 'schemas' AND entity_id = $1", eid,
        )
        await db.execute(
            """INSERT INTO schemas (id, name, kind, description, content)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (id) DO UPDATE SET
                 description = EXCLUDED.description, content = EXCLUDED.content""",
            eid, "update-e2e-schema", "agent", "Original description", "Original content",
        )
        # Ensure queue entry exists
        await db.execute(
            "INSERT INTO embedding_queue (table_name, entity_id, field_name, status)"
            " VALUES ('schemas', $1, 'description', 'pending')"
            " ON CONFLICT (table_name, entity_id, field_name)"
            " DO UPDATE SET status = 'pending'",
            eid,
        )
        emb_v1 = await wait_for_embedding(db, "embeddings_schemas", eid)
        assert emb_v1 is not None
        # Ensure v1 queue is fully processed before triggering v2
        await wait_for_queue_clear(db, "schemas", eid)

        kv_v1 = await db.fetchrow(
            "SELECT content_summary FROM kv_store WHERE entity_id = $1", eid,
        )
        assert kv_v1["content_summary"] == "Original content"

        # --- UPDATE ---
        await db.execute(
            """INSERT INTO schemas (id, name, kind, description, content)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (id) DO UPDATE SET
                 description = EXCLUDED.description, content = EXCLUDED.content""",
            eid, "update-e2e-schema", "agent", "Updated description", "Updated content",
        )

        # KV updated immediately
        kv_v2 = await db.fetchrow(
            "SELECT content_summary FROM kv_store WHERE entity_id = $1", eid,
        )
        assert kv_v2["content_summary"] == "Updated content"

        # Wait for worker to re-embed, verify new hash
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            emb_v2 = await db.fetchrow(
                "SELECT content_hash FROM embeddings_schemas WHERE entity_id = $1", eid,
            )
            if emb_v2 and emb_v2["content_hash"] != emb_v1["content_hash"]:
                break
            await asyncio.sleep(WORKER_POLL)
        assert emb_v2["content_hash"] != emb_v1["content_hash"]

    async def test_kv_only_table(self, db, embedding_worker):
        """servers: has_kv_sync=true, has_embeddings=false → KV yes, queue no."""
        eid = det_id("kv-only-server")
        await db.execute(
            """INSERT INTO servers (id, name, description) VALUES ($1, $2, $3)
               ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
            eid, "kv-only-server", "An MCP server for tools",
        )

        kv = await db.fetchrow(
            "SELECT * FROM kv_store WHERE entity_id = $1 AND entity_type = 'servers'", eid,
        )
        assert kv is not None
        assert kv["content_summary"] == "An MCP server for tools"

        eq = await db.fetchrow(
            "SELECT * FROM embedding_queue WHERE table_name = 'servers' AND entity_id = $1", eid,
        )
        assert eq is None

    async def test_no_artifacts_table(self, db, embedding_worker):
        """feedback: has_kv_sync=false, has_embeddings=false → neither artifact."""
        sess_id = det_id("fb-sess")
        await db.execute(
            """INSERT INTO sessions (id, name) VALUES ($1, $2)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name""",
            sess_id, "fb-sess",
        )
        fid = det_id("fb-entry")
        await db.execute(
            """INSERT INTO feedback (id, session_id, rating, comment)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET comment = EXCLUDED.comment""",
            fid, sess_id, 5, "Great answer",
        )

        assert await db.fetchrow(
            "SELECT 1 FROM kv_store WHERE entity_id = $1", fid,
        ) is None
        assert await db.fetchrow(
            "SELECT 1 FROM embedding_queue WHERE entity_id = $1", fid,
        ) is None

    async def test_multiple_tables_one_batch(self, db, embedding_worker):
        """Upserts across 3 tables — worker processes them all automatically."""
        s_id = det_id("batch-schema")
        o_id = det_id("batch-ontology")
        r_id = det_id("batch-resource")

        await db.execute(
            """INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
            s_id, "batch-schema", "model", "Schema desc",
        )
        await db.execute(
            """INSERT INTO ontologies (id, name, content) VALUES ($1, $2, $3)
               ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content""",
            o_id, "batch-ontology", "Ontology content",
        )
        await db.execute(
            """INSERT INTO resources (id, name, content) VALUES ($1, $2, $3)
               ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content""",
            r_id, "batch-resource", "Resource content",
        )

        assert await wait_for_embedding(db, "embeddings_schemas", s_id)
        assert await wait_for_embedding(db, "embeddings_ontologies", o_id)
        assert await wait_for_embedding(db, "embeddings_resources", r_id)


# ---------------------------------------------------------------------------
# Local provider unit tests
# ---------------------------------------------------------------------------


class TestLocalProvider:
    async def test_deterministic(self):
        provider = LocalEmbeddingProvider(dimensions=1536)
        assert await provider.embed(["same"]) == await provider.embed(["same"])

    async def test_different_texts_differ(self):
        provider = LocalEmbeddingProvider(dimensions=1536)
        assert await provider.embed(["a"]) != await provider.embed(["b"])

    async def test_batch(self):
        provider = LocalEmbeddingProvider(dimensions=1536)
        result = await provider.embed(["one", "two", "three"])
        assert len(result) == 3
        assert all(len(v) == 1536 for v in result)

    async def test_unit_normalized(self):
        provider = LocalEmbeddingProvider(dimensions=1536)
        [vec] = await provider.embed(["test"])
        assert abs(sum(v * v for v in vec) ** 0.5 - 1.0) < 1e-6

    async def test_provider_name(self):
        assert LocalEmbeddingProvider().provider_name == "local"


# ---------------------------------------------------------------------------
# Batch edge cases
# ---------------------------------------------------------------------------


class TestBatchEdgeCases:
    async def test_empty_queue(self, db, embedding_service):
        result = await embedding_service.process_batch()
        assert result == {"processed": 0, "skipped": 0, "failed": 0}

    async def test_null_content_skipped(self, db, embedding_worker):
        """NULL embedded field → skipped and removed from queue by worker."""
        eid = det_id("null-desc-schema")
        await db.execute(
            """INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)
               ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind""",
            eid, "null-desc-schema", "model",
        )
        cleared = await wait_for_queue_clear(db, "schemas", eid)
        assert cleared, "Worker should clear null-content queue entries"
        assert await db.fetchrow("SELECT 1 FROM embeddings_schemas WHERE entity_id=$1", eid) is None

    async def test_content_hash_cache(self, db, embedding_worker):
        """Re-queue same content → skipped by worker (hash match)."""
        eid = det_id("hash-cache-test")
        await db.execute(
            """INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
            eid, "hash-cache-test", "agent", "Stable description",
        )
        emb = await wait_for_embedding(db, "embeddings_schemas", eid)
        assert emb is not None
        original_hash = emb["content_hash"]

        # Re-queue manually (simulates trigger re-fire)
        await db.execute(
            """INSERT INTO embedding_queue (table_name, entity_id, field_name, status)
               VALUES ('schemas', $1, 'description', 'pending')
               ON CONFLICT (table_name, entity_id, field_name)
               DO UPDATE SET status = 'pending'""",
            eid,
        )
        cleared = await wait_for_queue_clear(db, "schemas", eid)
        assert cleared

        # Embedding unchanged — same hash
        emb2 = await db.fetchrow(
            "SELECT content_hash FROM embeddings_schemas WHERE entity_id = $1", eid,
        )
        assert emb2["content_hash"] == original_hash

    async def test_no_requeue_on_unrelated_field_change(self, db):
        """Updating a non-embedded field does NOT re-queue."""
        eid = det_id("no-requeue-schema")
        await db.execute(
            """INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)
               ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description""",
            eid, "no-requeue-schema", "model", "Stable",
        )
        await db.execute("DELETE FROM embedding_queue WHERE entity_id=$1", eid)
        await db.execute("UPDATE schemas SET kind='agent' WHERE id=$1", eid)
        assert await db.fetchrow("SELECT 1 FROM embedding_queue WHERE entity_id=$1", eid) is None


# ---------------------------------------------------------------------------
# Schema registration (kind='table') correctness
# ---------------------------------------------------------------------------


class TestTableSchemas:
    async def test_all_tables_registered(self, db):
        rows = await db.fetch(
            "SELECT name FROM schemas WHERE kind='table' AND deleted_at IS NULL"
        )
        names = {r["name"] for r in rows}
        expected = {
            "schemas", "ontologies", "resources", "moments", "sessions",
            "messages", "servers", "tools", "users", "files", "feedback",
            "tenants", "storage_grants",
        }
        assert expected.issubset(names)

    async def test_embedding_fields_correct(self, db):
        rows = await db.fetch(
            "SELECT name, json_schema FROM schemas"
            " WHERE kind='table' AND deleted_at IS NULL"
            "   AND (json_schema->>'has_embeddings')::boolean = true"
        )
        cfg = {r["name"]: r["json_schema"] for r in rows}
        assert cfg["schemas"]["embedding_field"] == "description"
        assert cfg["ontologies"]["embedding_field"] == "content"
        assert cfg["moments"]["embedding_field"] == "summary"
        assert cfg["files"]["embedding_field"] == "parsed_content"
        assert "servers" not in cfg

    async def test_kv_sync_config(self, db):
        rows = await db.fetch(
            "SELECT name FROM schemas"
            " WHERE kind='table' AND deleted_at IS NULL"
            "   AND (json_schema->>'has_kv_sync')::boolean = true"
        )
        kv_tables = {r["name"] for r in rows}
        assert "schemas" in kv_tables
        assert "servers" in kv_tables
        assert "messages" not in kv_tables
        assert "feedback" not in kv_tables


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from p8.api.main import app
    with TestClient(app) as c:
        yield c


class TestEmbeddingAPI:
    def test_process_endpoint(self, client):
        resp = client.post("/embeddings/process")
        assert resp.status_code == 200
        assert "processed" in resp.json()

    def test_generate_endpoint(self, client):
        resp = client.post("/embeddings/generate", json={"texts": ["hello", "world"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "local"
        assert data["count"] == 2
        assert len(data["embeddings"][0]) == 1536
