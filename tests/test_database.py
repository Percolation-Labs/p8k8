"""Tests for database bootstrap, triggers, and REM functions."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from tests.conftest import det_id


@pytest.mark.asyncio
async def test_extensions(db):
    rows = await db.fetch(
        "SELECT extname FROM pg_extension WHERE extname IN ('uuid-ossp','vector','pg_trgm','pg_cron')"
    )
    names = {r["extname"] for r in rows}
    assert names >= {"uuid-ossp", "vector", "pg_trgm", "pg_cron"}


@pytest.mark.asyncio
async def test_entity_tables(db):
    expected = {
        "schemas", "ontologies", "resources", "moments", "sessions",
        "messages", "servers", "tools", "users", "files", "feedback", "storage_grants",
    }
    rows = await db.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )
    names = {r["tablename"] for r in rows}
    assert expected <= names


@pytest.mark.asyncio
async def test_rem_functions(db):
    rows = await db.fetch(
        "SELECT routine_name FROM information_schema.routines"
        " WHERE routine_schema='public' AND routine_name LIKE 'rem_%'"
    )
    names = {r["routine_name"] for r in rows}
    assert names >= {"rem_lookup", "rem_search", "rem_fuzzy", "rem_traverse", "rem_load_messages"}


@pytest.mark.asyncio
async def test_kv_auto_sync(db, clean_db):
    """Insert a schema row — KV entry should appear via trigger."""
    sid = det_id("schemas", "test-agent")
    await db.execute(
        "INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)"
        " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
        sid, "test-agent", "agent", "A test agent",
    )
    kv = await db.fetchrow(
        "SELECT * FROM kv_store WHERE entity_id = $1", sid
    )
    assert kv is not None
    assert kv["entity_key"] == "test-agent"
    assert kv["entity_type"] == "schemas"


@pytest.mark.asyncio
async def test_kv_soft_delete(db, clean_db):
    sid = det_id("schemas", "temp-schema")
    # Ensure clean state (not soft-deleted from prior run)
    await db.execute(
        "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET deleted_at = NULL",
        sid, "temp-schema", "model",
    )
    # Soft delete
    await db.execute(
        "UPDATE schemas SET deleted_at = CURRENT_TIMESTAMP WHERE id = $1", sid
    )
    kv = await db.fetchrow("SELECT * FROM kv_store WHERE entity_id = $1", sid)
    assert kv is None


@pytest.mark.asyncio
async def test_embedding_queue(db, clean_db):
    """Insert a resource with content — should appear in embedding_queue."""
    rid = det_id("resources", "test-doc-eq")
    # Clear stale data so trigger fires fresh
    await db.execute("DELETE FROM embedding_queue WHERE entity_id = $1", rid)
    await db.execute("DELETE FROM embeddings_resources WHERE entity_id = $1", rid)
    await db.execute("DELETE FROM resources WHERE id = $1", rid)
    await db.execute(
        "INSERT INTO resources (id, name, content) VALUES ($1, $2, $3)",
        rid, "test-doc-eq", "Some content to embed",
    )
    row = await db.fetchrow(
        "SELECT * FROM embedding_queue WHERE entity_id = $1", rid
    )
    assert row is not None
    assert row["table_name"] == "resources"
    assert row["field_name"] == "content"
    assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_schema_timemachine(db, clean_db):
    sid = det_id("schemas", "versioned-agent")
    # Clear prior data (hard delete + timemachine entries)
    await db.execute("DELETE FROM schemas WHERE id = $1", sid)
    await db.execute("DELETE FROM schema_timemachine WHERE schema_id = $1", sid)
    # Fresh insert + update
    await db.execute(
        "INSERT INTO schemas (id, name, kind, content) VALUES ($1, $2, $3, $4)",
        sid, "versioned-agent", "agent", "prompt v1",
    )
    await db.execute(
        "UPDATE schemas SET content = $1 WHERE id = $2", "prompt v2", sid
    )
    rows = await db.fetch(
        "SELECT * FROM schema_timemachine WHERE schema_id = $1 ORDER BY recorded_at", sid
    )
    assert len(rows) >= 2
    assert rows[0]["operation"] == "INSERT"
    assert rows[1]["operation"] == "UPDATE"


@pytest.mark.asyncio
async def test_rem_lookup(db, clean_db):
    sid = det_id("schemas", "My Cool Agent")
    await db.execute(
        "INSERT INTO schemas (id, name, kind, description) VALUES ($1, $2, $3, $4)"
        " ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description",
        sid, "My Cool Agent", "agent", "Does cool things",
    )
    results = await db.rem_lookup("my-cool-agent")
    assert len(results) >= 1
    assert any(r["entity_type"] == "schemas" for r in results)


@pytest.mark.asyncio
async def test_rem_fuzzy(db, clean_db):
    sid = det_id("schemas", "data-analysis-agent")
    await db.execute(
        "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
        sid, "data-analysis-agent", "agent",
    )
    results = await db.rem_fuzzy("data analysis")
    assert len(results) >= 1
    assert any(r["entity_type"] == "schemas" for r in results)


@pytest.mark.asyncio
async def test_rem_traverse(db, clean_db):
    s1 = det_id("schemas", "parent-schema")
    s2 = det_id("schemas", "child-schema")
    edges = [{"target": "child-schema", "relation": "depends_on", "weight": 1.0}]
    await db.execute(
        "INSERT INTO schemas (id, name, kind, graph_edges) VALUES ($1, $2, $3, $4)"
        " ON CONFLICT (id) DO UPDATE SET graph_edges = EXCLUDED.graph_edges",
        s1, "parent-schema", "model", edges,
    )
    await db.execute(
        "INSERT INTO schemas (id, name, kind) VALUES ($1, $2, $3)"
        " ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind",
        s2, "child-schema", "model",
    )
    results = await db.rem_traverse("parent-schema", max_depth=1)
    keys = [r["entity_key"] for r in results]
    assert "parent-schema" in keys
    assert "child-schema" in keys


@pytest.mark.asyncio
async def test_rem_load_messages(db, clean_db):
    session_id = det_id("sessions", "db-load-msg-session")
    # Clean up prior run data for this session
    await db.execute("DELETE FROM messages WHERE session_id = $1", session_id)
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        session_id, "db-load-msg-session",
    )
    # Insert messages with known token counts
    for i in range(10):
        await db.execute(
            "INSERT INTO messages (id, session_id, message_type, content, token_count)"
            " VALUES ($1, $2, $3, $4, $5)",
            uuid4(), session_id, "user" if i % 2 == 0 else "assistant",
            f"Message {i}", 100,
        )

    # No limits — returns all 10
    all_msgs = await db.rem_load_messages(session_id)
    assert len(all_msgs) == 10

    # Token budget of 500 — should get 5 messages (5 × 100 = 500)
    by_tokens = await db.rem_load_messages(session_id, max_tokens=500)
    assert len(by_tokens) == 5

    # Max 3 messages
    by_count = await db.rem_load_messages(session_id, max_messages=3)
    assert len(by_count) == 3

    # Both limits — whichever is more restrictive wins
    both = await db.rem_load_messages(session_id, max_tokens=500, max_messages=3)
    assert len(both) == 3  # message limit is tighter

    # Chronological order (oldest first)
    assert all_msgs[0]["content"] == "Message 0"
    assert all_msgs[-1]["content"] == "Message 9"


@pytest.mark.asyncio
async def test_clone_session(db, clean_db):
    """Clone a session — copies session row + messages with new IDs."""
    uid1 = UUID("00000000-0000-0000-0000-000000000001")
    session_id = det_id("sessions", "db-clone-original")
    # Clean prior clone artifacts (messages for clones + original, then sessions)
    clone_ids = await db.fetch(
        "SELECT id FROM sessions WHERE name LIKE 'db-clone-original%' AND id != $1", session_id
    )
    for row in clone_ids:
        await db.execute("DELETE FROM messages WHERE session_id = $1", row["id"])
    await db.execute("DELETE FROM messages WHERE session_id = $1", session_id)
    await db.execute("DELETE FROM sessions WHERE name LIKE 'db-clone-original%'")
    await db.execute(
        "INSERT INTO sessions (id, name, agent_name, user_id) VALUES ($1, $2, $3, $4)",
        session_id, "db-clone-original", "query-agent", uid1,
    )
    for i in range(6):
        await db.execute(
            "INSERT INTO messages (id, session_id, message_type, content, token_count, user_id)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            uuid4(), session_id,
            "user" if i % 2 == 0 else "assistant",
            f"Message {i}", 50, uid1,
        )

    # Clone all messages
    result = await db.clone_session(session_id)
    assert result["messages_copied"] == 6
    new_id = result["new_session_id"]
    assert new_id != session_id

    # Verify session cloned
    clone = await db.fetchrow("SELECT * FROM sessions WHERE id = $1", new_id)
    assert clone["name"] == "db-clone-original (clone)"
    assert clone["agent_name"] == "query-agent"
    assert clone["user_id"] == uid1
    assert clone["total_tokens"] == 300  # 6 × 50

    # Verify messages cloned
    msgs = await db.fetch(
        "SELECT * FROM messages WHERE session_id = $1 ORDER BY created_at", new_id
    )
    assert len(msgs) == 6
    contents = {m["content"] for m in msgs}
    assert contents == {f"Message {i}" for i in range(6)}
    # All messages have new IDs
    orig_ids = {r["id"] for r in await db.fetch(
        "SELECT id FROM messages WHERE session_id = $1", session_id
    )}
    clone_ids = {m["id"] for m in msgs}
    assert orig_ids.isdisjoint(clone_ids)


@pytest.mark.asyncio
async def test_clone_session_with_limit_and_new_user(db, clean_db):
    """Clone with max_messages and new_user_id."""
    old_uid = UUID("00000000-0000-0000-0000-000000000010")
    new_uid = UUID("00000000-0000-0000-0000-000000000020")
    session_id = det_id("sessions", "db-clone-limit")
    # Clean prior clone artifacts (messages for clones + original, then sessions)
    clone_ids = await db.fetch(
        "SELECT id FROM sessions WHERE name LIKE 'db-clone-limit%' AND id != $1", session_id
    )
    for row in clone_ids:
        await db.execute("DELETE FROM messages WHERE session_id = $1", row["id"])
    await db.execute("DELETE FROM messages WHERE session_id = $1", session_id)
    await db.execute("DELETE FROM sessions WHERE name LIKE 'db-clone-limit%'")
    await db.execute(
        "INSERT INTO sessions (id, name, user_id) VALUES ($1, $2, $3)",
        session_id, "db-clone-limit", old_uid,
    )
    for i in range(10):
        await db.execute(
            "INSERT INTO messages (id, session_id, message_type, content, token_count, user_id)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            uuid4(), session_id, "user", f"Msg {i}", 100, old_uid,
        )

    result = await db.clone_session(
        session_id, max_messages=3, new_user_id=new_uid, new_agent_name="eval-agent"
    )
    assert result["messages_copied"] == 3
    new_id = result["new_session_id"]

    clone = await db.fetchrow("SELECT * FROM sessions WHERE id = $1", new_id)
    assert clone["user_id"] == new_uid
    assert clone["agent_name"] == "eval-agent"

    msgs = await db.fetch(
        "SELECT * FROM messages WHERE session_id = $1 ORDER BY created_at", new_id
    )
    assert len(msgs) == 3
    # All copied messages have the new user_id
    assert all(m["user_id"] == new_uid for m in msgs)
    # All came from the source session's content
    assert all(m["content"].startswith("Msg ") for m in msgs)


@pytest.mark.asyncio
async def test_search_sessions_by_name(db, clean_db):
    """Search sessions by name pattern."""
    PREFIX = "tssbn-eval-run-"
    uid1 = UUID("00000000-0000-0000-0000-000000000001")
    uid2 = UUID("00000000-0000-0000-0000-000000000002")
    await db.execute("DELETE FROM sessions WHERE name LIKE $1", f"{PREFIX}%")
    await db.execute("DELETE FROM sessions WHERE name = 'tssbn-unrelated-chat'")
    for i in range(5):
        await db.execute(
            "INSERT INTO sessions (id, name, agent_name, user_id) VALUES ($1, $2, $3, $4)",
            uuid4(), f"{PREFIX}{i}", "query-agent", uid1,
        )
    await db.execute(
        "INSERT INTO sessions (id, name, agent_name, user_id) VALUES ($1, $2, $3, $4)",
        uuid4(), "tssbn-unrelated-chat", "chat-agent", uid2,
    )

    result = await db.search_sessions(query=PREFIX)
    assert result["total"] == 5
    assert len(result["results"]) == 5
    assert all(PREFIX in r["name"] for r in result["results"])


@pytest.mark.asyncio
async def test_search_sessions_by_user_and_agent(db, clean_db):
    """Filter sessions by user_id and agent_name."""
    PREFIX = "tssbua-session-"
    tester_uid = UUID("00000000-0000-0000-0000-000000000099")
    other_uid = UUID("00000000-0000-0000-0000-000000000098")
    await db.execute("DELETE FROM sessions WHERE name LIKE $1", f"{PREFIX}%")
    await db.execute("DELETE FROM sessions WHERE name = 'tssbua-other-session'")
    for i in range(3):
        await db.execute(
            "INSERT INTO sessions (id, name, agent_name, user_id) VALUES ($1, $2, $3, $4)",
            uuid4(), f"{PREFIX}{i}", "tssbua-eval-agent", tester_uid,
        )
    await db.execute(
        "INSERT INTO sessions (id, name, agent_name, user_id) VALUES ($1, $2, $3, $4)",
        uuid4(), "tssbua-other-session", "tssbua-eval-agent", other_uid,
    )

    result = await db.search_sessions(user_id=tester_uid, agent_name="tssbua-eval-agent")
    assert result["total"] == 3
    assert all(r["user_id"] == tester_uid for r in result["results"])


@pytest.mark.asyncio
async def test_search_sessions_by_tags(db, clean_db):
    """Filter sessions by tag containment."""
    await db.execute("DELETE FROM sessions WHERE name IN ('tssbt-tagged-session', 'tssbt-other-tags')")
    await db.execute(
        "INSERT INTO sessions (id, name, tags) VALUES ($1, $2, $3)",
        uuid4(), "tssbt-tagged-session", ["tssbt-eval", "gpt4"],
    )
    await db.execute(
        "INSERT INTO sessions (id, name, tags) VALUES ($1, $2, $3)",
        uuid4(), "tssbt-other-tags", ["prod"],
    )

    result = await db.search_sessions(tags=["tssbt-eval"])
    assert result["total"] == 1
    assert result["results"][0]["name"] == "tssbt-tagged-session"


@pytest.mark.asyncio
async def test_search_sessions_pagination(db, clean_db):
    """Pagination returns correct pages and total count."""
    PREFIX = "tssp-page-test-"
    await db.execute("DELETE FROM sessions WHERE name LIKE $1", f"{PREFIX}%")
    for i in range(25):
        await db.execute(
            "INSERT INTO sessions (id, name) VALUES ($1, $2)",
            uuid4(), f"{PREFIX}{i:02d}",
        )

    page1 = await db.search_sessions(query=PREFIX, page=1, page_size=10)
    assert page1["total"] == 25
    assert len(page1["results"]) == 10
    assert page1["page"] == 1

    page3 = await db.search_sessions(query=PREFIX, page=3, page_size=10)
    assert page3["total"] == 25
    assert len(page3["results"]) == 5  # 25 - 20 = 5 remaining

    # No overlap between pages
    ids_p1 = {r["id"] for r in page1["results"]}
    ids_p3 = {r["id"] for r in page3["results"]}
    assert ids_p1.isdisjoint(ids_p3)


@pytest.mark.asyncio
async def test_search_sessions_message_count(db, clean_db):
    """Results include message_count per session."""
    sid = det_id("sessions", "tssmc-msg-count-session")
    await db.execute("DELETE FROM messages WHERE session_id = $1", sid)
    await db.execute(
        "INSERT INTO sessions (id, name) VALUES ($1, $2)"
        " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        sid, "tssmc-msg-count-session",
    )
    for i in range(7):
        await db.execute(
            "INSERT INTO messages (id, session_id, message_type, content) VALUES ($1, $2, $3, $4)",
            uuid4(), sid, "user", f"msg {i}",
        )

    result = await db.search_sessions(query="tssmc-msg-count")
    assert result["total"] == 1
    assert result["results"][0]["message_count"] == 7
