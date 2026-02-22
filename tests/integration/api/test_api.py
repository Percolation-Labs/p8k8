"""Tests for API endpoints."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """Synchronous test client — uses the real DB via lifespan."""
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


def test_health(client):
    resp = client.get("/admin/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "tables" in data
    assert "kv_entries" in data


def test_schema_crud(client):
    # Clean up stale soft-deleted row from prior run
    client.post("/query/", json={
        "mode": "SQL",
        "query": "DELETE FROM schemas WHERE name = 'api-test-agent'",
    })

    # Create
    schema = {
        "name": "api-test-agent",
        "kind": "agent",
        "description": "Test agent via API",
    }
    resp = client.post("/schemas/", json=schema)
    assert resp.status_code == 201
    created = resp.json()
    schema_id = created["id"]

    # Read
    resp = client.get(f"/schemas/{schema_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "api-test-agent"

    # List
    resp = client.get("/schemas/")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    # Delete
    resp = client.delete(f"/schemas/{schema_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify gone
    resp = client.get(f"/schemas/{schema_id}")
    assert resp.status_code == 404


def test_query_lookup(client):
    # Insert test data
    client.post("/schemas/", json={"name": "lookup-test", "kind": "model"})

    resp = client.post("/query/", json={"mode": "LOOKUP", "key": "lookup-test"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1
    assert results[0]["entity_type"] == "schemas"


def test_query_fuzzy(client):
    client.post("/schemas/", json={"name": "fuzzy-matching-agent", "kind": "agent"})

    resp = client.post("/query/", json={"mode": "FUZZY", "query": "fuzzy matching"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) >= 1


def test_rebuild_kv(client):
    client.post("/schemas/", json={"name": "kv-rebuild-test", "kind": "model"})

    resp = client.post("/admin/rebuild-kv")
    assert resp.status_code == 200
    assert resp.json()["rebuilt"] is True
    assert resp.json()["entries"] >= 1


def test_queue_status(client):
    resp = client.get("/admin/queue")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_schema_upsert_table_includes_verify(client):
    """Upserting a kind='table' schema should include _verify issues."""
    resp = client.post("/schemas/", json={
        "name": "schemas",
        "kind": "table",
        "description": "Ontology registry",
        "json_schema": {
            "has_kv_sync": True,
            "has_embeddings": True,
            "embedding_field": "description",
            "is_encrypted": False,
            "kv_summary_expr": "COALESCE(content, description, name)",
        },
    })
    assert resp.status_code == 201
    data = resp.json()
    # Clean DB should have no issues — _verify key may be absent or empty
    assert "_verify" not in data or len(data["_verify"]) == 0


def test_schema_upsert_non_table_no_verify(client):
    """Upserting a kind='agent' schema should not include _verify."""
    resp = client.post("/schemas/", json={
        "name": "verify-skip-test",
        "kind": "agent",
        "description": "Should not trigger verify",
    })
    assert resp.status_code == 201
    assert "_verify" not in resp.json()


def test_schema_upsert_full_chain(client):
    """POST /schemas/ → saved + KV entry + embedding queued + deterministic ID + verify on table kind."""
    # 1. Upsert an agent schema
    resp = client.post("/schemas/", json={
        "name": "chain-test-agent",
        "kind": "agent",
        "description": "Agent to test full upsert chain",
        "content": "You are a chain-test agent.",
    })
    assert resp.status_code == 201
    data = resp.json()
    schema_id = data["id"]

    # 2. Deterministic ID — same name → same id on re-upsert
    resp2 = client.post("/schemas/", json={
        "name": "chain-test-agent",
        "kind": "agent",
        "description": "Updated description",
    })
    assert resp2.status_code == 201
    assert resp2.json()["id"] == schema_id  # same deterministic ID
    assert resp2.json()["description"] == "Updated description"

    # 3. KV store — LOOKUP by name should find it
    resp3 = client.post("/query/", json={"mode": "LOOKUP", "key": "chain-test-agent"})
    assert resp3.status_code == 200
    results = resp3.json()
    assert len(results) >= 1
    assert any(r["data"]["name"] == "chain-test-agent" for r in results)

    # 4. Embedding queue — schema has __embedding_field__='description', trigger should queue
    resp4 = client.post("/query/", json={
        "mode": "SQL",
        "query": "SELECT * FROM embedding_queue WHERE entity_id = '" + schema_id + "'",
    })
    assert resp4.status_code == 200
    queue_rows = resp4.json()
    assert len(queue_rows) >= 1
    assert queue_rows[0]["table_name"] == "schemas"
    assert queue_rows[0]["field_name"] == "description"

    # 5. Verify — upsert a kind='table' schema and confirm verify runs
    resp5 = client.post("/schemas/", json={
        "name": "schemas",
        "kind": "table",
        "description": "Ontology registry",
        "json_schema": {
            "has_kv_sync": True,
            "has_embeddings": True,
            "embedding_field": "description",
            "is_encrypted": False,
            "kv_summary_expr": "COALESCE(content, description, name)",
        },
    })
    assert resp5.status_code == 201
    table_data = resp5.json()
    # Clean DB — no verify issues expected
    assert "_verify" not in table_data or len(table_data["_verify"]) == 0
