"""Tests for Repository.upsert() — bulk jsonb_populate_recordset pipeline.

Covers all 13 entity models, every field type, encryption, and edge cases
for the model_dump → _jsonify → jsonb_populate_recordset → RETURNING * →
_decrypt_row → model_validate pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from p8.ontology.types import (
    Feedback,
    File,
    Message,
    Moment,
    Resource,
    Schema,
    Server,
    Session,
    StorageGrant,
    Tool,
    User,
)
from p8.services.repository import Repository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


@pytest_asyncio.fixture
async def session_id(db):
    """Pre-existing session row for Message FK."""
    from tests.conftest import det_id
    sid = det_id("sessions", "repo-test-session")
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        sid, "repo-test-session",
    )
    return sid


@pytest_asyncio.fixture
async def user_row(db, encryption):
    """Pre-existing user row for StorageGrant FK."""
    from tests.conftest import det_id
    uid = det_id("users", "repo-fixture-user")
    await db.execute(
        "INSERT INTO users (id, name) VALUES ($1, $2)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        uid, "repo-fixture-user",
    )
    return uid


@pytest_asyncio.fixture
async def server_row(db, encryption):
    """Pre-existing server row for Tool FK."""
    from tests.conftest import det_id
    sid = det_id("servers", "repo-fixture-server")
    await db.execute(
        "INSERT INTO servers (id, name, url) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET url = EXCLUDED.url",
        sid, "repo-fixture-server", "https://mcp.example.com",
    )
    return sid


# ---------------------------------------------------------------------------
# 1. Single Entity Upsert — Type Round-Trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_schema(db, encryption):
    """Schema: json_schema (JSONB), kind, version, tags, graph_edges."""
    repo = Repository(Schema, db, encryption)
    s = Schema(
        name="test-agent",
        kind="agent",
        version="1.0.0",
        description="An agent for testing",
        content="You are a test agent.",
        json_schema={
            "model_name": "anthropic:claude-sonnet-4-5-20250929",
            "tools": [{"name": "search", "server": "rem", "protocol": "mcp"}],
            "temperature": 0.3,
        },
        tags=["test", "agent"],
        graph_edges=[{"target": "query-agent", "relation": "delegates_to", "weight": 0.8}],
        metadata={"priority": 1, "active": True},
    )
    [result] = await repo.upsert(s)

    assert result.name == "test-agent"
    assert result.kind == "agent"
    assert result.version == "1.0.0"
    assert result.description == "An agent for testing"
    assert result.content == "You are a test agent."
    assert result.json_schema["model_name"] == "anthropic:claude-sonnet-4-5-20250929"
    assert len(result.json_schema["tools"]) == 1
    assert result.json_schema["tools"][0]["name"] == "search"
    assert result.json_schema["temperature"] == 0.3
    assert result.tags == ["test", "agent"]
    assert result.graph_edges[0]["target"] == "query-agent"
    assert result.graph_edges[0]["weight"] == 0.8
    assert result.metadata["priority"] == 1
    assert result.metadata["active"] is True
    assert result.id == s.id


@pytest.mark.asyncio
async def test_single_server(db, encryption):
    """Server: auth_config (JSONB), enabled (bool), protocol."""
    repo = Repository(Server, db, encryption)
    s = Server(
        name="openai-server",
        url="https://api.openai.com/v1",
        protocol="openapi",
        auth_config={"type": "bearer", "token_env": "OPENAI_API_KEY"},
        enabled=True,
        description="OpenAI API server",
    )
    [result] = await repo.upsert(s)

    assert result.name == "openai-server"
    assert result.url == "https://api.openai.com/v1"
    assert result.protocol == "openapi"
    assert result.auth_config == {"type": "bearer", "token_env": "OPENAI_API_KEY"}
    assert result.enabled is True
    assert result.description == "OpenAI API server"


@pytest.mark.asyncio
async def test_single_tool(db, encryption, server_row):
    """Tool: server_id (UUID FK), input_schema/output_schema (JSONB), enabled=False."""
    repo = Repository(Tool, db, encryption)
    t = Tool(
        name="search",
        server_id=server_row,
        description="Semantic search tool",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "array", "items": {"type": "object"}},
        enabled=False,
    )
    [result] = await repo.upsert(t)

    assert result.name == "search"
    assert result.server_id == server_row
    assert result.description == "Semantic search tool"
    assert result.input_schema["type"] == "object"
    assert result.output_schema["type"] == "array"
    assert result.enabled is False


@pytest.mark.asyncio
async def test_single_resource(db, encryption):
    """Resource: ordinal (int), related_entities (text[]), category."""
    repo = Repository(Resource, db, encryption)
    r = Resource(
        name="chapter-3",
        uri="s3://docs/chapter-3.md",
        ordinal=3,
        content="Chapter 3: Advanced Topics",
        category="documentation",
        related_entities=["chapter-1", "chapter-2", "appendix-a"],
        tags=["docs", "advanced"],
    )
    [result] = await repo.upsert(r)

    assert result.name == "chapter-3"
    assert result.uri == "s3://docs/chapter-3.md"
    assert result.ordinal == 3
    assert result.content == "Chapter 3: Advanced Topics"
    assert result.category == "documentation"
    assert result.related_entities == ["chapter-1", "chapter-2", "appendix-a"]
    assert result.tags == ["docs", "advanced"]


@pytest.mark.asyncio
async def test_single_file(db, encryption):
    """File: size_bytes (BIGINT), parsed_output (JSONB), mime_type."""
    repo = Repository(File, db, encryption)
    f = File(
        name="report.pdf",
        uri="s3://uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=2_500_000,
        parsed_content="Quarterly revenue increased by 15%...",
        parsed_output={
            "pages": 42,
            "headings": ["Introduction", "Q1 Results", "Outlook"],
            "tables": [{"rows": 10, "cols": 4}],
        },
        tags=["finance", "quarterly"],
    )
    [result] = await repo.upsert(f)

    assert result.name == "report.pdf"
    assert result.mime_type == "application/pdf"
    assert result.size_bytes == 2_500_000
    assert result.parsed_content == "Quarterly revenue increased by 15%..."
    assert result.parsed_output["pages"] == 42
    assert len(result.parsed_output["headings"]) == 3
    assert result.parsed_output["tables"][0]["rows"] == 10


@pytest.mark.asyncio
async def test_single_moment(db, encryption, session_id):
    """Moment: starts/ends_timestamp (datetime), present_persons (JSONB list[dict]),
    emotion_tags/topic_tags (text[]), source_session_id (UUID FK)."""
    repo = Repository(Moment, db, encryption)
    now = datetime.now(UTC)
    m = Moment(
        name="standup-2025-01-15",
        moment_type="meeting",
        summary="Team standup: discussed sprint progress",
        starts_timestamp=now,
        ends_timestamp=now + timedelta(minutes=30),
        present_persons=[
            {"name": "Alice", "role": "lead"},
            {"name": "Bob", "role": "developer"},
        ],
        emotion_tags=["productive", "focused"],
        topic_tags=["sprint-review", "blockers"],
        category="standup",
        source_session_id=session_id,
        previous_moment_keys=["standup-2025-01-14"],
    )
    [result] = await repo.upsert(m)

    assert result.name == "standup-2025-01-15"
    assert result.moment_type == "meeting"
    assert result.summary == "Team standup: discussed sprint progress"
    assert result.starts_timestamp is not None
    assert result.ends_timestamp is not None
    assert result.ends_timestamp > result.starts_timestamp
    assert len(result.present_persons) == 2
    assert result.present_persons[0]["name"] == "Alice"
    assert result.present_persons[1]["role"] == "developer"
    assert result.emotion_tags == ["productive", "focused"]
    assert result.topic_tags == ["sprint-review", "blockers"]
    assert result.category == "standup"
    assert result.source_session_id == session_id
    assert result.previous_moment_keys == ["standup-2025-01-14"]


@pytest.mark.asyncio
async def test_single_storage_grant(db, encryption, user_row):
    """StorageGrant: user_id_ref (UUID FK), auto_sync (bool), last_sync_at, sync_mode."""
    repo = Repository(StorageGrant, db, encryption)
    now = datetime.now(UTC)
    sg = StorageGrant(
        user_id_ref=user_row,
        provider="google-drive",
        provider_folder_id="1AbCdEfGh",
        folder_name="My Documents",
        folder_path="/My Drive/My Documents",
        sync_mode="full",
        auto_sync=False,
        last_sync_at=now,
        sync_cursor="page_token_xyz",
        status="paused",
    )
    [result] = await repo.upsert(sg)

    assert result.user_id_ref == user_row
    assert result.provider == "google-drive"
    assert result.provider_folder_id == "1AbCdEfGh"
    assert result.folder_name == "My Documents"
    assert result.folder_path == "/My Drive/My Documents"
    assert result.sync_mode == "full"
    assert result.auto_sync is False
    assert result.last_sync_at is not None
    assert result.sync_cursor == "page_token_xyz"
    assert result.status == "paused"


# ---------------------------------------------------------------------------
# 2. Bulk Upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_schemas(db, encryption):
    """5 schemas with varying kinds, tags, json_schema dicts."""
    repo = Repository(Schema, db, encryption)
    schemas = [
        Schema(name="agent-1", kind="agent", tags=["prod"], json_schema={"model_name": "gpt-4"}),
        Schema(name="eval-1", kind="evaluator", tags=["eval"], json_schema={"metric": "accuracy"}),
        Schema(name="model-1", kind="model", tags=["base"]),
        Schema(name="tool-def-1", kind="tool", description="A tool def", json_schema={"input": {}}),
        Schema(name="resource-def-1", kind="resource", version="2.0"),
    ]
    results = await repo.upsert(schemas)

    assert len(results) == 5
    names = {r.name for r in results}
    assert names == {"agent-1", "eval-1", "model-1", "tool-def-1", "resource-def-1"}
    kinds = {r.name: r.kind for r in results}
    assert kinds["agent-1"] == "agent"
    assert kinds["eval-1"] == "evaluator"


@pytest.mark.asyncio
async def test_bulk_resources_ordered(db, encryption):
    """3 ordered doc chunks with ordinals 1-3."""
    repo = Repository(Resource, db, encryption)
    chunks = [
        Resource(name="doc-chunk-1", ordinal=1, content="Introduction"),
        Resource(name="doc-chunk-2", ordinal=2, content="Body"),
        Resource(name="doc-chunk-3", ordinal=3, content="Conclusion"),
    ]
    results = await repo.upsert(chunks)

    assert len(results) == 3
    by_name = {r.name: r for r in results}
    assert by_name["doc-chunk-1"].ordinal == 1
    assert by_name["doc-chunk-2"].ordinal == 2
    assert by_name["doc-chunk-3"].ordinal == 3


@pytest.mark.asyncio
async def test_bulk_servers_then_tools(db, encryption):
    """2 servers + 4 tools referencing those server_ids."""
    server_repo = Repository(Server, db, encryption)
    servers = [
        Server(name="mcp-server", url="https://mcp.example.com"),
        Server(name="openapi-server", url="https://api.example.com", protocol="openapi"),
    ]
    [s1, s2] = await server_repo.upsert(servers)

    tool_repo = Repository(Tool, db, encryption)
    tools = [
        Tool(name="search", server_id=s1.id, description="Search via MCP"),
        Tool(name="action", server_id=s1.id, description="Action via MCP"),
        Tool(name="list-users", server_id=s2.id, description="List users via REST"),
        Tool(name="get-user", server_id=s2.id, description="Get user via REST"),
    ]
    results = await tool_repo.upsert(tools)

    assert len(results) == 4
    mcp_tools = [r for r in results if r.server_id == s1.id]
    api_tools = [r for r in results if r.server_id == s2.id]
    assert len(mcp_tools) == 2
    assert len(api_tools) == 2


# ---------------------------------------------------------------------------
# 3. JSONB Deep Round-Trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonb_nested_json_schema(db, encryption):
    """Deeply nested json_schema with arrays of objects."""
    repo = Repository(Schema, db, encryption)
    deep_schema = {
        "model_name": "anthropic:claude-sonnet-4-5-20250929",
        "temperature": 0.3,
        "tools": [
            {
                "name": "search",
                "server": "rem",
                "protocol": "mcp",
                "config": {
                    "filters": [
                        {"field": "category", "op": "eq", "value": "document"},
                        {"field": "score", "op": "gte", "value": 0.7},
                    ],
                    "nested": {"deep": {"level": [1, 2, 3]}},
                },
            }
        ],
        "response_schema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    s = Schema(name="deep-schema", kind="agent", json_schema=deep_schema)
    [result] = await repo.upsert(s)

    assert result.json_schema["tools"][0]["config"]["filters"][1]["value"] == 0.7
    assert result.json_schema["tools"][0]["config"]["nested"]["deep"]["level"] == [1, 2, 3]
    assert result.json_schema["response_schema"]["properties"]["sources"]["type"] == "array"


@pytest.mark.asyncio
async def test_jsonb_tool_calls(db, encryption, session_id):
    """Message tool_calls — complex tool invocation structure."""
    repo = Repository(Message, db, encryption)
    tool_calls = {
        "calls": [
            {
                "id": "call_001",
                "name": "rem_search",
                "arguments": {"query": "machine learning", "table": "resources", "limit": 5},
                "result": {"matches": 3, "ids": ["a", "b", "c"]},
            },
            {
                "id": "call_002",
                "name": "ask_agent",
                "arguments": {"agent": "analyst", "prompt": "summarize"},
            },
        ],
        "total_duration_ms": 1250,
    }
    m = Message(
        session_id=session_id,
        message_type="tool_call",
        tool_calls=tool_calls,
    )
    [result] = await repo.upsert(m)

    assert result.tool_calls["calls"][0]["name"] == "rem_search"
    assert result.tool_calls["calls"][0]["arguments"]["limit"] == 5
    assert result.tool_calls["calls"][1]["id"] == "call_002"
    assert result.tool_calls["total_duration_ms"] == 1250


@pytest.mark.asyncio
async def test_jsonb_empty_objects(db, encryption):
    """Empty JSONB values survive (not None)."""
    repo = Repository(Schema, db, encryption)
    s = Schema(
        name="empty-jsonb",
        json_schema={},
        metadata={},
        graph_edges=[],
        tags=[],
    )
    [result] = await repo.upsert(s)

    assert result.json_schema == {}
    assert result.metadata == {}
    assert result.graph_edges == []
    assert result.tags == []


# ---------------------------------------------------------------------------
# 4. graph_edges and metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_edges_with_weights(db, encryption):
    """Schema with 2 edges — target, relation, weight round-trip."""
    repo = Repository(Schema, db, encryption)
    s = Schema(
        name="linked-schema",
        graph_edges=[
            {"target": "query-agent", "relation": "delegates_to", "weight": 0.9},
            {"target": "summarizer", "relation": "uses", "weight": 0.5},
        ],
    )
    [result] = await repo.upsert(s)

    assert len(result.graph_edges) == 2
    assert result.graph_edges[0]["target"] == "query-agent"
    assert result.graph_edges[0]["relation"] == "delegates_to"
    assert result.graph_edges[0]["weight"] == 0.9
    assert result.graph_edges[1]["target"] == "summarizer"
    assert result.graph_edges[1]["weight"] == 0.5


@pytest.mark.asyncio
async def test_metadata_deeply_nested(db, encryption):
    """Resource with nested dicts, bools, arrays, numbers in metadata."""
    repo = Repository(Resource, db, encryption)
    r = Resource(
        name="rich-metadata",
        metadata={
            "source": {
                "provider": "google-drive",
                "folder": {"id": "abc", "path": "/docs"},
            },
            "flags": {"reviewed": True, "archived": False},
            "scores": [0.95, 0.87, 0.92],
            "count": 42,
            "label": None,
        },
    )
    [result] = await repo.upsert(r)

    assert result.metadata["source"]["folder"]["path"] == "/docs"
    assert result.metadata["flags"]["reviewed"] is True
    assert result.metadata["flags"]["archived"] is False
    assert result.metadata["scores"] == [0.95, 0.87, 0.92]
    assert result.metadata["count"] == 42
    assert result.metadata["label"] is None


# ---------------------------------------------------------------------------
# 5. Update via Upsert
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_changes_fields(db, encryption):
    """Insert schema, upsert same id with new description + version → updated."""
    repo = Repository(Schema, db, encryption)
    s = Schema(name="evolving", description="v1", version="1.0")
    [v1] = await repo.upsert(s)
    assert v1.description == "v1"
    assert v1.version == "1.0"

    # Update: same id, new fields
    s.description = "v2 with improvements"
    s.version = "2.0"
    [v2] = await repo.upsert(s)

    assert v2.id == v1.id
    assert v2.description == "v2 with improvements"
    assert v2.version == "2.0"
    assert v2.name == "evolving"  # unchanged


@pytest.mark.asyncio
async def test_update_preserves_unset_fields(db, encryption):
    """COALESCE preserves existing values when upserting partial entity."""
    repo = Repository(Server, db, encryption)
    s = Server(
        name="preserved-server",
        url="https://old.example.com",
        description="Original description",
        auth_config={"type": "bearer", "token": "secret"},
    )
    [original] = await repo.upsert(s)

    # Upsert with only url changed — create new instance with same id
    # Note: auth_config defaults to {} (not None) so it IS included in
    # model_dump(exclude_none=True) and COALESCE picks the non-NULL EXCLUDED value.
    # Only truly None fields (like description below) are preserved via COALESCE.
    partial = Server(id=s.id, name="preserved-server", url="https://new.example.com")
    [updated] = await repo.upsert(partial)

    assert updated.id == original.id
    assert updated.url == "https://new.example.com"
    # description is truly None in partial → COALESCE preserves existing
    assert updated.description == "Original description"
    # auth_config defaults to {} → COALESCE picks EXCLUDED (non-NULL {})
    assert updated.auth_config == {}


# ---------------------------------------------------------------------------
# 6. Mixed Insert + Update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_insert_and_update(db, encryption):
    """Insert A and B, then bulk upsert [A-modified, C-new, B-modified] → 3 results."""
    repo = Repository(Schema, db, encryption)
    a = Schema(name="alpha", description="a-original")
    b = Schema(name="beta", description="b-original")
    await repo.upsert([a, b])

    # Modify A and B, add new C
    a.description = "a-updated"
    b.description = "b-updated"
    c = Schema(name="gamma", description="c-new")

    results = await repo.upsert([a, b, c])
    assert len(results) == 3

    by_name = {r.name: r for r in results}
    assert by_name["alpha"].description == "a-updated"
    assert by_name["beta"].description == "b-updated"
    assert by_name["gamma"].description == "c-new"
    # A and B kept their original ids
    assert by_name["alpha"].id == a.id
    assert by_name["beta"].id == b.id


# ---------------------------------------------------------------------------
# 7. Minimal/Partial Entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minimal_schema(db, encryption):
    """Schema(name='bare') — all defaults correct."""
    repo = Repository(Schema, db, encryption)
    [result] = await repo.upsert(Schema(name="bare"))

    assert result.name == "bare"
    assert result.kind == "model"  # default
    assert result.version is None
    assert result.description is None
    assert result.content is None
    assert result.json_schema is None
    assert result.tags == []
    assert result.metadata == {}
    assert result.graph_edges == []
    assert result.deleted_at is None
    assert result.tenant_id is None
    assert result.user_id is None


@pytest.mark.asyncio
async def test_minimal_session(db, encryption):
    """Session with only a name — all other optional fields default correctly."""
    repo = Repository(Session, db, encryption)
    # name is required for KV store trigger (entity_key derived from name)
    [result] = await repo.upsert(Session(name="bare-session"))

    assert result.name == "bare-session"
    assert result.description is None
    assert result.agent_name is None
    assert result.mode is None
    assert result.total_tokens == 0


# ---------------------------------------------------------------------------
# 8. Empty List
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_list(db, encryption):
    """upsert([]) returns []."""
    repo = Repository(Schema, db, encryption)
    results = await repo.upsert([])
    assert results == []


# ---------------------------------------------------------------------------
# 9. Encrypted Models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encrypted_message_roundtrip(db, encryption, session_id):
    """Message content encrypted in DB, decrypted in returned model."""
    tenant = "repo-enc-tenant"
    await encryption.get_dek(tenant)

    # Session needs tenant_id for FK
    await db.execute(
        "UPDATE sessions SET tenant_id = $1 WHERE id = $2", tenant, session_id
    )

    repo = Repository(Message, db, encryption)
    m = Message(
        session_id=session_id,
        message_type="user",
        content="This should be encrypted at rest",
        tenant_id=tenant,
    )
    [result] = await repo.upsert(m)

    # Returned model has plaintext (decrypted in pipeline)
    assert result.content == "This should be encrypted at rest"

    # DB has ciphertext
    raw = await db.fetchrow("SELECT content FROM messages WHERE id = $1", result.id)
    assert raw["content"] != "This should be encrypted at rest"

    # get() also decrypts
    loaded = await repo.get(result.id, tenant_id=tenant)
    assert loaded.content == "This should be encrypted at rest"


@pytest.mark.asyncio
async def test_encrypted_user_email_deterministic(db, encryption):
    """User email (deterministic) + content (randomized) both decrypt correctly."""
    tenant = "repo-det-tenant"
    await encryption.get_dek(tenant)

    repo = Repository(User, db, encryption)
    u = User(
        name="Alice",
        email="alice@example.com",
        content="Alice is a software engineer",
        tenant_id=tenant,
    )
    [result] = await repo.upsert(u)

    assert result.email == "alice@example.com"
    assert result.content == "Alice is a software engineer"

    # Verify DB has ciphertext for both
    raw = await db.fetchrow("SELECT email, content FROM users WHERE id = $1", result.id)
    assert raw["email"] != "alice@example.com"
    assert raw["content"] != "Alice is a software engineer"


@pytest.mark.asyncio
async def test_no_encryption_without_tenant(db, encryption):
    """User without tenant_id → plaintext in DB."""
    repo = Repository(User, db, encryption)
    u = User(name="Public", email="public@example.com", content="Public bio")
    [result] = await repo.upsert(u)

    raw = await db.fetchrow("SELECT email, content FROM users WHERE id = $1", result.id)
    assert raw["email"] == "public@example.com"
    assert raw["content"] == "Public bio"


@pytest.mark.asyncio
async def test_encrypted_bulk_feedback(db, encryption, session_id):
    """3 Feedback with comments in one bulk call, all encrypt/decrypt."""
    tenant = "repo-bulk-enc"
    await encryption.get_dek(tenant)

    repo = Repository(Feedback, db, encryption)
    feedbacks = [
        Feedback(session_id=session_id, rating=5, comment="Excellent answer", tenant_id=tenant),
        Feedback(session_id=session_id, rating=3, comment="Could be better", tenant_id=tenant),
        Feedback(session_id=session_id, rating=1, comment="Completely wrong", tenant_id=tenant),
    ]
    results = await repo.upsert(feedbacks)

    assert len(results) == 3
    comments = {r.comment for r in results}
    assert comments == {"Excellent answer", "Could be better", "Completely wrong"}

    # Verify all encrypted in DB
    for result in results:
        raw = await db.fetchrow("SELECT comment FROM feedback WHERE id = $1", result.id)
        assert raw["comment"] != result.comment  # ciphertext in DB


# ---------------------------------------------------------------------------
# 10. Multi-Tenant Bulk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_tenant_bulk(db, encryption):
    """2 users with different tenant_ids + own keys, each decrypts correctly."""
    tenant_a = "repo-multi-a"
    tenant_b = "repo-multi-b"
    await encryption.configure_tenant(tenant_a, enabled=True, own_key=True)
    await encryption.configure_tenant(tenant_b, enabled=True, own_key=True)

    repo = Repository(User, db, encryption)
    users = [
        User(name="Alice", content="Alice bio", tenant_id=tenant_a),
        User(name="Bob", content="Bob bio", tenant_id=tenant_b),
    ]
    results = await repo.upsert(users)

    assert len(results) == 2
    alice = next(r for r in results if r.name == "Alice")
    bob = next(r for r in results if r.name == "Bob")

    assert alice.content == "Alice bio"
    assert bob.content == "Bob bio"

    # Each tenant's own key decrypts their own data
    loaded_a = await repo.get(alice.id, tenant_id=tenant_a)
    assert loaded_a.content == "Alice bio"
    loaded_b = await repo.get(bob.id, tenant_id=tenant_b)
    assert loaded_b.content == "Bob bio"

    # Cross-tenant can't decrypt
    cross = await repo.get(alice.id, tenant_id=tenant_b)
    assert cross.content != "Alice bio"


@pytest.mark.asyncio
async def test_mixed_tenant_and_public(db, encryption):
    """One tenanted user (encrypted) + one public user (plaintext) in same batch."""
    tenant = "repo-mixed-tenant"
    await encryption.get_dek(tenant)

    repo = Repository(User, db, encryption)
    users = [
        User(name="Tenanted", content="Secret bio", tenant_id=tenant),
        User(name="Public", content="Public bio"),
    ]
    results = await repo.upsert(users)

    assert len(results) == 2
    tenanted = next(r for r in results if r.name == "Tenanted")
    public = next(r for r in results if r.name == "Public")

    assert tenanted.content == "Secret bio"
    assert public.content == "Public bio"

    # Verify DB state
    raw_t = await db.fetchrow("SELECT content FROM users WHERE id = $1", tenanted.id)
    assert raw_t["content"] != "Secret bio"  # encrypted
    raw_p = await db.fetchrow("SELECT content FROM users WHERE id = $1", public.id)
    assert raw_p["content"] == "Public bio"  # plaintext


# ---------------------------------------------------------------------------
# 11. Large Batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_batch_50(db, encryption):
    """50 schemas in one call — all inserted, all names match."""
    repo = Repository(Schema, db, encryption)
    schemas = [Schema(name=f"batch-{i:03d}", kind="model") for i in range(50)]
    results = await repo.upsert(schemas)

    assert len(results) == 50
    result_names = {r.name for r in results}
    expected_names = {f"batch-{i:03d}" for i in range(50)}
    assert result_names == expected_names


# ---------------------------------------------------------------------------
# 12. FK Constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_bad_session_fk(db, encryption):
    """Message with non-existent session_id → ForeignKeyViolation."""
    repo = Repository(Message, db, encryption)
    m = Message(session_id=uuid4(), message_type="user", content="orphan")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await repo.upsert(m)


@pytest.mark.asyncio
async def test_storage_grant_bad_user_fk(db, encryption):
    """StorageGrant with non-existent user_id_ref → ForeignKeyViolation."""
    repo = Repository(StorageGrant, db, encryption)
    sg = StorageGrant(user_id_ref=uuid4(), provider="google-drive")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await repo.upsert(sg)


# ---------------------------------------------------------------------------
# 13. Return Type + Read After Write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_return_type(db, encryption):
    """Upsert returns model instances, not dicts or base CoreModel."""
    repo = Repository(Schema, db, encryption)
    [result] = await repo.upsert(Schema(name="typed"))

    assert isinstance(result, Schema)
    assert type(result) is Schema  # exact type, not a subclass


@pytest.mark.asyncio
async def test_get_after_upsert(db, encryption):
    """Upsert then get() → all fields match."""
    repo = Repository(Schema, db, encryption)
    s = Schema(
        name="get-test",
        kind="agent",
        version="1.0",
        description="test description",
        content="system prompt",
        json_schema={"model_name": "gpt-4"},
        tags=["a", "b"],
        metadata={"key": "value"},
    )
    [upserted] = await repo.upsert(s)
    loaded = await repo.get(upserted.id)

    assert loaded is not None
    assert loaded.id == upserted.id
    assert loaded.name == "get-test"
    assert loaded.kind == "agent"
    assert loaded.version == "1.0"
    assert loaded.description == "test description"
    assert loaded.content == "system prompt"
    assert loaded.json_schema == {"model_name": "gpt-4"}
    assert loaded.tags == ["a", "b"]
    assert loaded.metadata == {"key": "value"}


@pytest.mark.asyncio
async def test_find_by_tags_after_upsert(db, encryption):
    """Upsert 3 with tag + 2 without, find(tags=['x']) returns 3."""
    repo = Repository(Schema, db, encryption)
    tagged = [
        Schema(name="tagged-1", tags=["x", "y"]),
        Schema(name="tagged-2", tags=["x"]),
        Schema(name="tagged-3", tags=["x", "z"]),
    ]
    untagged = [
        Schema(name="untagged-1", tags=["y"]),
        Schema(name="untagged-2"),
    ]
    await repo.upsert(tagged + untagged)

    results = await repo.find(tags=["x"])
    assert len(results) == 3
    names = {r.name for r in results}
    assert names == {"tagged-1", "tagged-2", "tagged-3"}
