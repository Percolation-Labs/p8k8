"""Integration tests for the chained_tool feature.

Tests that an agent with structured_output + chained_tool auto-invokes
the named tool with the structured output after the agent run.

End-to-end flow:
  1. Register a structured output agent with chained_tool="action"
  2. Run it via ask_agent (non-streaming)
  3. Verify the agent produces structured output
  4. Verify the chained tool was called and its result is in the response
  5. Verify tool_call + tool_response messages are persisted in the session
"""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic_ai.models.test import TestModel

USER_ADA = UUID("00000000-0000-0000-0000-00000000ada0")


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tools_ready(db, encryption):
    """Initialize tools module with DB and encryption."""
    from p8.api.tools import init_tools

    init_tools(db, encryption)


# ---------------------------------------------------------------------------
# Schema round-trip — chained_tool field persists
# ---------------------------------------------------------------------------


def test_chained_tool_field_in_schema():
    """chained_tool field is present and defaults to None."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.build(
        name="plain-agent",
        description="No chaining.",
    )
    assert schema.chained_tool is None


def test_chained_tool_field_set():
    """chained_tool can be set explicitly."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.build(
        name="chained-agent",
        description="Agent with chaining.",
        structured_output=True,
        chained_tool="save_moments",
    )
    assert schema.chained_tool == "save_moments"
    assert schema.structured_output is True


def test_chained_tool_yaml_roundtrip():
    """chained_tool survives YAML serialization."""
    from p8.agentic.agent_schema import AgentSchema

    original = AgentSchema.build(
        name="yaml-chain-agent",
        description="YAML chaining test.",
        structured_output=True,
        chained_tool="action",
    )
    yaml_str = original.to_yaml()
    loaded = AgentSchema.from_yaml(yaml_str)
    assert loaded.chained_tool == "action"
    assert loaded.structured_output is True


def test_chained_tool_dict_roundtrip():
    """chained_tool survives dict serialization."""
    from p8.agentic.agent_schema import AgentSchema

    original = AgentSchema.build(
        name="dict-chain-agent",
        description="Dict chaining test.",
        structured_output=True,
        chained_tool="save_moments",
    )
    d = original.to_dict()
    assert d["chained_tool"] == "save_moments"
    loaded = AgentSchema.from_dict(d)
    assert loaded.chained_tool == "save_moments"


async def test_chained_tool_db_roundtrip(db, encryption):
    """chained_tool persists through DB save/load."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    original = AgentSchema.build(
        name="db-chain-agent",
        description="DB chaining test.",
        structured_output=True,
        chained_tool="save_moments",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**original.to_schema_dict()))

    adapter = await AgentAdapter.from_schema_name("db-chain-agent", db, encryption)
    assert adapter.agent_schema.chained_tool == "save_moments"
    assert adapter.agent_schema.structured_output is True


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


def test_tool_registry_lookup():
    """get_tool_fn returns callable for known tools."""
    from p8.api.tools import get_tool_fn

    fn = get_tool_fn("action")
    assert fn is not None
    assert callable(fn)


def test_tool_registry_missing():
    """get_tool_fn returns None for unknown tools."""
    from p8.api.tools import get_tool_fn

    fn = get_tool_fn("nonexistent_tool_xyz")
    assert fn is None


# ---------------------------------------------------------------------------
# execute_chained_tool — unit-level
# ---------------------------------------------------------------------------


async def test_execute_chained_tool_no_chaining(db, encryption):
    """Returns None when agent has no chained_tool."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="no-chain-agent",
        description="No chaining.",
        structured_output=True,
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("no-chain-agent", db, encryption)

    result = await adapter.execute_chained_tool({"key": "value"})
    assert result is None


async def test_execute_chained_tool_not_structured(db, encryption):
    """Returns None when agent is not structured_output even if chained_tool set."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="not-structured-agent",
        description="Not structured.",
        structured_output=False,
        chained_tool="action",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("not-structured-agent", db, encryption)

    result = await adapter.execute_chained_tool({"key": "value"})
    assert result is None


async def test_execute_chained_tool_missing_tool(db, encryption):
    """Returns None with warning when chained tool not found in registry."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="missing-tool-agent",
        description="Missing tool.",
        structured_output=True,
        chained_tool="nonexistent_tool_xyz",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("missing-tool-agent", db, encryption)

    result = await adapter.execute_chained_tool({"key": "value"})
    assert result is None


async def test_execute_chained_tool_calls_action(db, encryption, tools_ready):
    """execute_chained_tool invokes the action tool and returns result."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="chain-action-agent",
        description="Chains to action.",
        structured_output=True,
        chained_tool="action",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("chain-action-agent", db, encryption)

    result = await adapter.execute_chained_tool(
        {"type": "observation", "payload": {"note": "chained"}},
    )
    assert result is not None
    assert result["status"] == "success"
    assert result["action_type"] == "observation"


async def test_execute_chained_tool_persists_messages(db, encryption, tools_ready):
    """execute_chained_tool persists tool_call + tool_response to session."""
    import json

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema, Session
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="chain-persist-agent",
        description="Chains and persists.",
        structured_output=True,
        chained_tool="action",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("chain-persist-agent", db, encryption)

    session_id = uuid4()
    session_repo = Repository(Session, db, encryption)
    await session_repo.upsert(Session(
        id=session_id, name="chain-test", agent_name="chain-persist-agent",
        user_id=USER_ADA,
    ))
    result = await adapter.execute_chained_tool(
        {"type": "observation", "payload": {"note": "persisted"}},
        session_id=session_id,
        user_id=USER_ADA,
    )
    assert result is not None

    # Verify messages were persisted
    rows = await db.fetch(
        "SELECT message_type, content, tool_calls FROM messages"
        " WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    assert len(rows) == 2

    # First row: tool_call
    assert rows[0]["message_type"] == "tool_call"
    tc = rows[0]["tool_calls"]
    if isinstance(tc, str):
        tc = json.loads(tc)
    assert tc["name"] == "action"
    assert tc["arguments"]["type"] == "observation"

    # Second row: tool_response
    assert rows[1]["message_type"] == "tool_response"
    resp = rows[1]["tool_calls"]
    if isinstance(resp, str):
        resp = json.loads(resp)
    assert resp["name"] == "action"


async def test_execute_chained_tool_error_handling(db, encryption, tools_ready):
    """execute_chained_tool catches errors and returns error dict."""
    from unittest.mock import AsyncMock

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    schema = AgentSchema.build(
        name="chain-error-agent",
        description="Will error.",
        structured_output=True,
        chained_tool="action",
    )
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("chain-error-agent", db, encryption)

    from p8.api.tools import TOOL_REGISTRY
    original = TOOL_REGISTRY.get("action")
    TOOL_REGISTRY["action"] = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        result = await adapter.execute_chained_tool(
            {"type": "observation", "payload": {}},
        )
        assert result is not None
        assert result["status"] == "error"
        assert "boom" in result["error"]
    finally:
        if original:
            TOOL_REGISTRY["action"] = original


# ---------------------------------------------------------------------------
# Integration: complex structured output → save_moments → DB verification
#
# This is the real proof: a dreaming-style agent produces complex nested
# structured output (moments with affinity fragments), the chained tool
# (save_moments) receives it, persists moments to the DB, and both the
# agent output and the tool return value are stored as tool_call/tool_response
# messages in the session.
# ---------------------------------------------------------------------------

# Realistic structured output matching what a dreaming agent produces.
# save_moments expects: moments: list[dict], user_id: UUID | None
DREAM_STRUCTURED_OUTPUT = {
    "moments": [
        {
            "name": "dream-api-gateway-ml-pipeline-convergence",
            "summary": (
                "## API Gateways Mirror ML Pipelines\n\n"
                "The **schema validation** in our `pandas` preprocessing pipeline and the "
                "**JWT validation** at the API gateway both enforce contracts at system "
                "boundaries. Same principle, different domain.\n\n"
                "### Threads\n"
                "- [ML pipeline discussion](moment://session-ml-chunk-0)\n"
                "- [API gateway ADR](resource://arch-doc-chunk-0000)"
            ),
            "topic_tags": ["api-gateway", "machine-learning", "validation", "architecture"],
            "emotion_tags": ["curious"],
            "affinity_fragments": [
                {
                    "target": "arch-doc-chunk-0000",
                    "relation": "thematic_link",
                    "weight": 0.85,
                    "reason": "Both enforce boundary validation at system edges",
                },
                {
                    "target": "ml-report-chunk-0000",
                    "relation": "builds_on",
                    "weight": 0.7,
                    "reason": "ML pipeline schema validation parallels gateway contract enforcement",
                },
            ],
        },
        {
            "name": "dream-event-driven-async-patterns",
            "summary": (
                "## Event-Driven Patterns Across Domains\n\n"
                "We see NATS JetStream for async microservice communication and "
                "event-driven triggers in ML training pipelines using the same "
                "decoupled message queue pattern.\n\n"
                "### Threads\n"
                "- [Architecture doc](resource://arch-doc-chunk-0000)"
            ),
            "topic_tags": ["async", "event-driven", "microservices"],
            "emotion_tags": [],
            "affinity_fragments": [
                {
                    "target": "arch-doc-chunk-0000",
                    "relation": "thematic_link",
                    "weight": 0.8,
                    "reason": "NATS JetStream async messaging pattern",
                },
            ],
        },
    ],
}


async def test_save_moments_chained_tool_integration(db, encryption, tools_ready):
    """Complex structured output → save_moments → moments in DB + messages persisted.

    This is the core integration test for chained_tool. It proves:
    1. A dreaming-style agent's complex structured output (nested moments with
       affinity_fragments) is piped directly into save_moments
    2. save_moments actually persists the moments to the moments table
    3. The tool_call message stores the full structured output as arguments
    4. The tool_response message stores the save_moments return value
       (including saved_moment_ids)
    """
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.api.tools import set_tool_context
    from p8.ontology.types import Schema, Session
    from p8.services.repository import Repository

    # Register a dreaming-style agent that chains to save_moments.
    # Properties match save_moments signature: moments (array), user_id (string).
    schema = AgentSchema._parse_dict({
        "type": "object",
        "name": "dream-chain-agent",
        "description": "Reflective agent that produces dream moments.",
        "structured_output": True,
        "chained_tool": "save_moments",
        "properties": {
            "moments": {
                "type": "array",
                "description": "Dream moments to persist",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "summary": {"type": "string"},
                        "topic_tags": {"type": "array", "items": {"type": "string"}},
                        "emotion_tags": {"type": "array", "items": {"type": "string"}},
                        "affinity_fragments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "target": {"type": "string"},
                                    "relation": {"type": "string"},
                                    "weight": {"type": "number"},
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
        "required": ["moments"],
    })
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))
    adapter = await AgentAdapter.from_schema_name("dream-chain-agent", db, encryption)

    # Create session + set tool context
    session_id = uuid4()
    session_repo = Repository(Session, db, encryption)
    await session_repo.upsert(Session(
        id=session_id, name="dream-chain-test", agent_name="dream-chain-agent",
        user_id=USER_ADA,
    ))
    set_tool_context(user_id=USER_ADA, session_id=session_id)

    # Execute chained tool with realistic dreaming output
    result = await adapter.execute_chained_tool(
        DREAM_STRUCTURED_OUTPUT,
        session_id=session_id,
        user_id=USER_ADA,
    )

    # ---------------------------------------------------------------
    # 1. save_moments actually saved the moments
    # ---------------------------------------------------------------
    assert result is not None
    assert result["status"] == "success"
    assert result["moments_count"] == 2
    assert len(result["saved_moment_ids"]) == 2

    # Verify moments exist in the DB
    moment_rows = await db.fetch(
        "SELECT name, moment_type, summary, topic_tags, graph_edges, user_id "
        "FROM moments WHERE user_id = $1 AND moment_type = 'dream' "
        "ORDER BY created_at",
        USER_ADA,
    )
    assert len(moment_rows) == 2

    # First moment: check name, summary, tags, graph_edges
    m1 = moment_rows[0]
    assert "Api Gateway Ml Pipeline Convergence" in m1["name"]
    assert "schema validation" in m1["summary"].lower()
    assert "api-gateway" in m1["topic_tags"]
    assert "machine-learning" in m1["topic_tags"]

    # graph_edges were converted from affinity_fragments
    edges1 = m1["graph_edges"]
    if isinstance(edges1, str):
        edges1 = json.loads(edges1)
    assert len(edges1) == 2
    targets = {e["target"] for e in edges1}
    assert "arch-doc-chunk-0000" in targets
    assert "ml-report-chunk-0000" in targets
    assert any(e["weight"] == 0.85 for e in edges1)

    # Second moment
    m2 = moment_rows[1]
    assert "Event Driven Async Patterns" in m2["name"]

    # ---------------------------------------------------------------
    # 2. tool_call message stores the full structured output
    # ---------------------------------------------------------------
    msg_rows = await db.fetch(
        "SELECT message_type, content, tool_calls FROM messages "
        "WHERE session_id = $1 ORDER BY created_at",
        session_id,
    )
    assert len(msg_rows) == 2  # tool_call + tool_response

    # tool_call row
    tc_row = msg_rows[0]
    assert tc_row["message_type"] == "tool_call"
    tc = tc_row["tool_calls"]
    if isinstance(tc, str):
        tc = json.loads(tc)
    assert tc["name"] == "save_moments"
    # arguments contain the full structured output including nested moments
    args = tc["arguments"]
    assert len(args["moments"]) == 2
    assert args["moments"][0]["name"] == "dream-api-gateway-ml-pipeline-convergence"
    assert len(args["moments"][0]["affinity_fragments"]) == 2
    assert args["moments"][0]["affinity_fragments"][0]["target"] == "arch-doc-chunk-0000"
    assert args["moments"][0]["affinity_fragments"][0]["weight"] == 0.85

    # ---------------------------------------------------------------
    # 3. tool_response message stores the save_moments return value
    # ---------------------------------------------------------------
    tr_row = msg_rows[1]
    assert tr_row["message_type"] == "tool_response"
    tr_meta = tr_row["tool_calls"]
    if isinstance(tr_meta, str):
        tr_meta = json.loads(tr_meta)
    assert tr_meta["name"] == "save_moments"
    # Same call_id links the pair
    assert tr_meta["id"] == tc["id"]

    # content has the save_moments return value
    resp = json.loads(tr_row["content"])
    assert resp["status"] == "success"
    assert resp["moments_count"] == 2
    assert len(resp["saved_moment_ids"]) == 2
    # saved_moment_ids are valid UUIDs
    for mid in resp["saved_moment_ids"]:
        UUID(mid)  # will raise if not a valid UUID


async def test_ask_agent_save_moments_chained_e2e(db, encryption, tools_ready):
    """Full ask_agent pipeline: structured agent → save_moments chained tool.

    Proves the end-to-end flow through ask_agent (non-streaming):
    agent.run() → structured output → execute_chained_tool → save_moments →
    moments in DB + messages in session.

    Uses a mock agent result to inject realistic complex output, because
    TestModel() generates empty lists for array fields.
    """
    from unittest.mock import AsyncMock, MagicMock

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.api.tools import set_tool_context
    from p8.api.tools.ask_agent import ask_agent
    from p8.ontology.types import Schema, Session
    from p8.services.repository import Repository

    # Register agent with chained_tool = save_moments
    schema = AgentSchema._parse_dict({
        "type": "object",
        "name": "ask-dream-agent",
        "description": "Dreaming agent for ask_agent e2e test.",
        "structured_output": True,
        "chained_tool": "save_moments",
        "properties": {
            "moments": {
                "type": "array",
                "description": "Dream moments",
                "items": {"type": "object"},
            },
        },
        "required": ["moments"],
    })
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**schema.to_schema_dict()))

    # Create session
    session_id = uuid4()
    session_repo = Repository(Session, db, encryption)
    await session_repo.upsert(Session(
        id=session_id, name="ask-dream-test", agent_name="ask-dream-agent",
        user_id=USER_ADA,
    ))
    set_tool_context(user_id=USER_ADA, session_id=session_id)

    # Mock agent.run() to return a known complex structured output.
    # This is necessary because TestModel() generates empty arrays.
    mock_output = MagicMock()
    mock_output.model_dump.return_value = DREAM_STRUCTURED_OUTPUT
    mock_result = MagicMock()
    mock_result.output = mock_output
    mock_result.all_messages.return_value = []

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        agent = original_build(self, model_override=TestModel(), **kwargs)
        agent.run = AsyncMock(return_value=mock_result)
        return agent

    with patch.object(AgentAdapter, "build_agent", patched_build):
        result = await ask_agent("ask-dream-agent", "Reflect on recent activity")

    # Agent output
    assert result["status"] == "success"
    assert result["is_structured_output"] is True
    assert result["output"]["moments"][0]["name"] == "dream-api-gateway-ml-pipeline-convergence"
    assert len(result["output"]["moments"][0]["affinity_fragments"]) == 2

    # Chained tool result
    assert result["chained_tool_result"] is not None
    assert result["chained_tool_result"]["status"] == "success"
    assert result["chained_tool_result"]["moments_count"] == 2

    # Verify moments in DB
    moment_rows = await db.fetch(
        "SELECT name, summary, graph_edges FROM moments "
        "WHERE user_id = $1 AND moment_type = 'dream' ORDER BY created_at",
        USER_ADA,
    )
    assert len(moment_rows) == 2
    assert "Api Gateway Ml Pipeline Convergence" in moment_rows[0]["name"]

    # Verify messages in session
    msg_rows = await db.fetch(
        "SELECT message_type, content, tool_calls FROM messages "
        "WHERE session_id = $1 AND message_type IN ('tool_call', 'tool_response') "
        "ORDER BY created_at",
        session_id,
    )
    # At least the chained tool pair
    tc_rows = [r for r in msg_rows if r["message_type"] == "tool_call"]
    tr_rows = [r for r in msg_rows if r["message_type"] == "tool_response"]
    assert len(tc_rows) >= 1
    assert len(tr_rows) >= 1

    # tool_call has save_moments with full structured output
    tc = tc_rows[-1]["tool_calls"]
    if isinstance(tc, str):
        tc = json.loads(tc)
    assert tc["name"] == "save_moments"
    assert len(tc["arguments"]["moments"]) == 2

    # tool_response has saved_moment_ids
    resp = json.loads(tr_rows[-1]["content"])
    assert resp["moments_count"] == 2
    assert len(resp["saved_moment_ids"]) == 2
