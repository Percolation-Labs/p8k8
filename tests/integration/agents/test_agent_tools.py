"""Integration tests for AgentAdapter with MCP tool loading.

Tests the full agent construction pipeline:
- Agent registration and loading (built-in, YAML, DB)
- AgentSchema from_model_class, from_yaml_file, from_schema_row
- Tool resolution from FastMCP server via FastMCPToolset
- Tool description suffixes in system prompt
- Thinking aide properties in prompt guidance
- DB and YAML round-trips
- TTL caching
- Delegation (ask_agent calling another agent)
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

USER_ADA = UUID("00000000-0000-0000-0000-00000000ada0")

import pytest
import pytest_asyncio
from pydantic_ai.models.test import TestModel


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def mcp_server(db, encryption):
    """Return the local FastMCP server with tools initialized."""
    from p8.api.mcp_server import get_mcp_server
    from p8.api.tools import init_tools

    init_tools(db, encryption)
    return get_mcp_server()


@pytest_asyncio.fixture
async def sample_adapter(db, encryption):
    """Register the SampleAgent and return its adapter."""
    from p8.agentic.adapter import AgentAdapter, register_sample_agent

    await register_sample_agent(db, encryption)
    return await AgentAdapter.from_schema_name("sample-agent", db, encryption)


@pytest_asyncio.fixture
async def test_user(db, encryption):
    """Create a test user in the database."""
    from p8.ontology.types import User
    from p8.services.repository import Repository

    repo = Repository(User, db, encryption)
    user = User(
        name="Ada Lovelace",
        email="ada@example.com",
        content="Pioneer of computing. Interested in algorithms and mathematics.",
        tags=["computing", "mathematics"],
        metadata={"role": "engineer"},
        user_id=USER_ADA,
    )
    [result] = await repo.upsert(user)
    return result


# ---------------------------------------------------------------------------
# AgentSchema.from_model_class
# ---------------------------------------------------------------------------


def test_from_model_class_general():
    """from_model_class parses GeneralAgent into a flat AgentSchema."""
    from p8.agentic.core_agents import GeneralAgent
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.from_model_class(GeneralAgent)
    assert schema.name == "general"
    assert schema.structured_output is False
    assert "friendly" in schema.description
    # Properties are thinking aides, not "answer"
    assert "user_intent" in schema.properties
    assert "topic" in schema.properties
    assert "requires_search" in schema.properties
    assert "answer" not in schema.properties
    # Tools are inline with descriptions
    tool_names = {t.name for t in schema.tools}
    assert {"search", "action", "ask_agent", "remind_me"}.issubset(tool_names)
    # Tool descriptions present
    search_tool = next(t for t in schema.tools if t.name == "search")
    assert search_tool.description is not None
    assert "REM" in search_tool.description


def test_from_model_class_dreaming():
    """from_model_class parses DreamingAgent with nested models."""
    from p8.agentic.core_agents import DreamingAgent
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.from_model_class(DreamingAgent)
    assert schema.name == "dreaming-agent"
    assert schema.structured_output is True
    assert schema.temperature == 0.7
    assert "dream_moments" in schema.properties
    assert "search_questions" in schema.properties
    assert "cross_session_themes" in schema.properties
    # Limits
    assert schema.limits is not None
    assert schema.limits.request_limit == 15
    assert schema.limits.total_tokens_limit == 115000


def test_from_model_class_sample():
    """from_model_class parses SampleAgent correctly."""
    from p8.agentic.core_agents import SampleAgent
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.from_model_class(SampleAgent)
    assert schema.name == "sample-agent"
    assert "topic" in schema.properties
    assert "requires_search" in schema.properties
    assert len(schema.tools) == 3
    assert schema.limits is not None
    assert schema.limits.request_limit == 10


# ---------------------------------------------------------------------------
# Tool description suffix in system prompt
# ---------------------------------------------------------------------------


def test_tool_notes_in_system_prompt():
    """Tools with descriptions get a 'Tool Notes' section in system prompt."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.build(
        name="test-agent",
        description="You help users.",
        tools=[
            {"name": "search", "description": "Query the KB using REM"},
            {"name": "action"},  # no description
        ],
    )
    prompt = schema.get_system_prompt()
    assert "## Tool Notes" in prompt
    assert "**search**: Query the KB using REM" in prompt
    # action has no description, should not appear in tool notes
    assert "**action**" not in prompt


def test_no_tool_notes_without_descriptions():
    """No Tool Notes section when no tools have descriptions."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.build(
        name="test-agent",
        description="You help users.",
        tools=[{"name": "search"}, {"name": "action"}],
    )
    prompt = schema.get_system_prompt()
    assert "## Tool Notes" not in prompt


# ---------------------------------------------------------------------------
# Thinking aide properties in prompt guidance
# ---------------------------------------------------------------------------


def test_thinking_structure_in_prompt():
    """Properties appear as 'Thinking Structure' in conversational mode prompt."""
    from p8.agentic.core_agents import GENERAL_AGENT

    prompt = GENERAL_AGENT.get_system_prompt()
    assert "## Thinking Structure" in prompt
    assert "user_intent" in prompt
    assert "topic" in prompt
    assert "requires_search" in prompt
    assert "Do NOT output field names" in prompt


def test_no_thinking_structure_in_structured_mode():
    """Structured output agents don't get thinking structure guidance."""
    from p8.agentic.core_agents import DREAMING_AGENT

    prompt = DREAMING_AGENT.get_system_prompt()
    assert "## Thinking Structure" not in prompt


# ---------------------------------------------------------------------------
# SampleAgent registration & config
# ---------------------------------------------------------------------------


async def test_register_sample_agent(db, encryption):
    """register_sample_agent creates a schema row with correct config."""
    from p8.agentic.adapter import register_sample_agent

    schema = await register_sample_agent(db, encryption)
    assert schema.name == "sample-agent"
    assert schema.kind == "agent"
    assert "knowledge base" in schema.content
    assert schema.json_schema is not None
    # Flat format: tools at top level of json_schema
    tool_names = {t["name"] if isinstance(t, dict) else t for t in schema.json_schema.get("tools", [])}
    assert {"search", "action", "ask_agent"}.issubset(tool_names)


async def test_builtin_agent_auto_registers(db, encryption):
    """from_schema_name auto-registers built-in agents on first load."""
    from p8.agentic.adapter import AgentAdapter

    adapter = await AgentAdapter.from_schema_name("sample-agent", db, encryption)
    assert adapter.schema.name == "sample-agent"


# ---------------------------------------------------------------------------
# Adapter .config property
# ---------------------------------------------------------------------------


async def test_adapter_config_property(sample_adapter):
    """adapter.config is an alias for adapter.agent_schema."""
    assert sample_adapter.config is sample_adapter.agent_schema
    assert sample_adapter.config.name == "sample-agent"
    assert sample_adapter.config.limits is not None
    assert sample_adapter.config.limits.request_limit == 10
    assert len(sample_adapter.config.tools) >= 3
    tool_names = {t.name for t in sample_adapter.config.tools}
    assert {"search", "action", "ask_agent"}.issubset(tool_names)


# ---------------------------------------------------------------------------
# YAML file loader
# ---------------------------------------------------------------------------


def test_load_yaml_agents_from_dir(tmp_path):
    """_load_yaml_agents loads .yaml and .yml files from schema_dir."""
    import yaml

    from p8.agentic import adapter

    (tmp_path / "greeter.yaml").write_text(yaml.dump({
        "name": "greeter",
        "kind": "agent",
        "description": "Says hello",
        "content": "You greet people.",
        "json_schema": {"temperature": 0.5},
    }))
    (tmp_path / "summarizer.yml").write_text(yaml.dump({
        "name": "summarizer",
        "kind": "agent",
        "description": "Summarizes text",
        "content": "You summarize things.",
        "json_schema": {"temperature": 0.2},
    }))
    (tmp_path / "notes.txt").write_text("not an agent")

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = str(tmp_path)
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

        assert "greeter" in adapter.BUILTIN_AGENTS
        assert "summarizer" in adapter.BUILTIN_AGENTS
        assert adapter.BUILTIN_AGENTS["greeter"]["content"] == "You greet people."
        assert adapter.BUILTIN_AGENTS["summarizer"]["json_schema"]["temperature"] == 0.2
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


def test_load_yaml_agents_flat_format(tmp_path):
    """_load_yaml_agents loads flat AgentSchema format YAML files."""
    from p8.agentic import adapter

    (tmp_path / "flat-agent.yaml").write_text("""\
type: object
name: flat-agent
description: A flat agent loaded from YAML.
properties:
  topic:
    type: string
    description: Main topic
tools:
  - name: search
    description: Search knowledge base
temperature: 0.5
""")

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = str(tmp_path)
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

        assert "flat-agent" in adapter.BUILTIN_AGENTS
        # Should be converted to Schema entity format
        d = adapter.BUILTIN_AGENTS["flat-agent"]
        assert d["kind"] == "agent"
        assert "flat agent" in d["content"].lower()
        assert d["json_schema"]["name"] == "flat-agent"
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


def test_load_yaml_agents_missing_dir():
    """_load_yaml_agents handles missing schema_dir gracefully."""
    from p8.agentic import adapter

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = "/nonexistent/path"
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

        assert "sample-agent" in adapter.BUILTIN_AGENTS
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


def test_load_yaml_agents_skips_invalid(tmp_path):
    """_load_yaml_agents skips files missing 'name' key."""
    import yaml

    from p8.agentic import adapter

    (tmp_path / "bad.yaml").write_text(yaml.dump({"description": "no name"}))
    (tmp_path / "good.yaml").write_text(yaml.dump({
        "name": "valid-agent",
        "kind": "agent",
        "content": "I work.",
    }))

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = str(tmp_path)
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

        assert "valid-agent" in adapter.BUILTIN_AGENTS
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


def test_load_yaml_does_not_overwrite_code_agents(tmp_path):
    """YAML agents don't overwrite code-defined agents."""
    import yaml

    from p8.agentic import adapter

    (tmp_path / "sample-agent.yaml").write_text(yaml.dump({
        "name": "sample-agent",
        "kind": "agent",
        "content": "I am the impostor.",
    }))

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = str(tmp_path)
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

        assert "knowledge base" in adapter.BUILTIN_AGENTS["sample-agent"]["content"]
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


async def test_yaml_agent_auto_registers(db, encryption, tmp_path):
    """A YAML-defined agent auto-registers on first from_schema_name lookup."""
    import yaml

    from p8.agentic import adapter
    from p8.agentic.adapter import AgentAdapter

    (tmp_path / "yaml-bot.yaml").write_text(yaml.dump({
        "name": "yaml-bot",
        "kind": "agent",
        "description": "Loaded from YAML",
        "content": "I was loaded from a YAML file.",
        "json_schema": {"temperature": 0.7, "max_tokens": 1000},
    }))

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import MagicMock, patch
        mock_settings = MagicMock()
        mock_settings.schema_dir = str(tmp_path)
        with patch("p8.settings.get_settings", return_value=mock_settings):
            adapter._load_yaml_agents()

            agent_adapter = await AgentAdapter.from_schema_name("yaml-bot", db, encryption)

        assert agent_adapter.schema.name == "yaml-bot"
        assert agent_adapter.config.temperature == 0.7
        assert "YAML file" in agent_adapter.schema.content
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


# ---------------------------------------------------------------------------
# DB round-trip
# ---------------------------------------------------------------------------


async def test_db_roundtrip_flat_schema(db, encryption):
    """Save a flat AgentSchema to DB and load it back with all fields intact."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.agent_schema import AgentSchema
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    # Build a schema programmatically
    original = AgentSchema.build(
        name="roundtrip-agent",
        description="Test agent for DB round-trip.",
        properties={
            "topic": {"type": "string", "description": "Main topic"},
        },
        tools=[
            {"name": "search", "description": "Search KB"},
            {"name": "action"},
        ],
        temperature=0.5,
        limits={"request_limit": 8, "total_tokens_limit": 40000},
    )

    # Save to DB
    repo = Repository(Schema, db, encryption)
    [row] = await repo.upsert(Schema(**original.to_schema_dict()))
    assert row.name == "roundtrip-agent"

    # Load back via adapter
    adapter = await AgentAdapter.from_schema_name("roundtrip-agent", db, encryption)
    loaded = adapter.agent_schema

    assert loaded.name == "roundtrip-agent"
    assert loaded.temperature == 0.5
    assert "topic" in loaded.properties
    assert len(loaded.tools) == 2
    assert loaded.limits is not None
    assert loaded.limits.request_limit == 8
    assert loaded.limits.total_tokens_limit == 40000

    # Verify tool description survived
    search_tool = next(t for t in loaded.tools if t.name == "search")
    assert search_tool.description == "Search KB"


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------


def test_yaml_roundtrip():
    """AgentSchema can be serialized to YAML and loaded back."""
    from p8.agentic.agent_schema import AgentSchema

    original = AgentSchema.build(
        name="yaml-rt-agent",
        description="YAML round-trip test.",
        tools=[
            {"name": "search", "description": "Search the KB"},
        ],
        temperature=0.3,
    )

    yaml_str = original.to_yaml()
    loaded = AgentSchema.from_yaml(yaml_str)

    assert loaded.name == "yaml-rt-agent"
    assert loaded.temperature == 0.3
    assert len(loaded.tools) == 1
    assert loaded.tools[0].name == "search"
    assert loaded.tools[0].description == "Search the KB"


def test_yaml_file_roundtrip():
    """AgentSchema loads from YAML file on disk."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema.from_yaml_file(
        Path(__file__).resolve().parents[3] / ".schema" / "sample-agent.yaml"
    )
    assert schema.name == "sample-agent"
    assert "knowledge base" in schema.description
    assert len(schema.tools) == 3
    assert schema.limits is not None
    assert schema.limits.request_limit == 10


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


async def test_adapter_cache_ttl(db, encryption):
    """AgentAdapter caches adapters with TTL."""
    from p8.agentic import adapter
    from p8.agentic.adapter import AgentAdapter, _adapter_cache, _cache_key

    await AgentAdapter.from_schema_name("sample-agent", db, encryption)

    key = _cache_key("sample-agent", None)
    assert key in _adapter_cache
    cached_adapter, cached_ts = _adapter_cache[key]
    assert cached_adapter.schema.name == "sample-agent"

    # Second call should return cached (same object)
    adapter2 = await AgentAdapter.from_schema_name("sample-agent", db, encryption)
    assert adapter2 is cached_adapter


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------


async def test_mcp_tool_names_exclude_delegates(sample_adapter):
    """MCP tool names exclude delegate tools (ask_agent)."""
    mcp_names = sample_adapter._get_mcp_tool_names()
    assert "search" in mcp_names
    assert "action" in mcp_names
    assert "ask_agent" not in mcp_names


async def test_delegate_tools_include_ask_agent(sample_adapter):
    """Delegate tools include ask_agent as a direct Python function."""
    delegate_tools = sample_adapter._get_delegate_tools()
    assert len(delegate_tools) == 1
    assert delegate_tools[0].__name__ == "ask_agent"


async def test_resolve_toolsets_with_local_mcp(sample_adapter, mcp_server):
    """resolve_toolsets creates a filtered FastMCPToolset from local server."""
    toolsets, tools = sample_adapter.resolve_toolsets(mcp_server=mcp_server)
    assert len(toolsets) == 1
    assert len(tools) == 1  # ask_agent


# ---------------------------------------------------------------------------
# FastMCPToolset — list and call tools
# ---------------------------------------------------------------------------


async def test_fastmcp_server_lists_tools(mcp_server):
    """FastMCP server exposes search, action, and ask_agent tools."""
    tools = await mcp_server.list_tools()
    tool_names = {t.name for t in tools}
    assert {"search", "action", "ask_agent"}.issubset(tool_names)


async def test_fastmcp_toolset_creates_from_server(mcp_server):
    """FastMCPToolset can be instantiated from a FastMCP server."""
    from pydantic_ai.toolsets.fastmcp import FastMCPToolset

    toolset = FastMCPToolset(mcp_server)
    allowed = {"search", "action"}
    filtered = toolset.filtered(lambda ctx, td: td.name in allowed)
    assert filtered is not None


async def test_fastmcp_call_search(db, encryption, mcp_server):
    """Call search tool directly through FastMCP server."""
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="mcp-test-entry",
        kind="model",
        description="Entry for MCP toolset test",
    ))

    tool = await mcp_server.get_tool("search")
    result = await tool.run({"query": "LOOKUP mcp-test-entry"})
    assert result is not None


async def test_fastmcp_call_action(mcp_server):
    """Call action tool directly through FastMCP server."""
    tool = await mcp_server.get_tool("action")
    result = await tool.run({"type": "observation", "payload": {"note": "test"}})
    assert result is not None


# ---------------------------------------------------------------------------
# Direct tool invocation — search
# ---------------------------------------------------------------------------


async def test_search_tool_lookup(db, encryption):
    """search tool executes LOOKUP queries."""
    from p8.api.tools import init_tools
    from p8.api.tools.search import search

    init_tools(db, encryption)

    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="test-ontology-entry",
        kind="model",
        description="A test entry for lookup",
    ))

    result = await search("LOOKUP test-ontology-entry")
    assert result["status"] == "success"
    assert result["count"] >= 1


async def test_search_tool_sql(db, encryption):
    """search tool executes SQL queries."""
    from p8.api.tools import init_tools
    from p8.api.tools.search import search

    init_tools(db, encryption)

    result = await search("SQL SELECT count(*) as cnt FROM schemas WHERE kind = 'table'")
    assert result["status"] == "success"
    assert result["count"] >= 1


# ---------------------------------------------------------------------------
# Direct tool invocation — action
# ---------------------------------------------------------------------------


async def test_action_tool_observation():
    """action tool emits observation events."""
    from p8.api.tools.action import action

    result = await action(
        type="observation",
        payload={"confidence": 0.95, "sources": ["test-entry"]},
    )
    assert result["status"] == "success"
    assert result["action_type"] == "observation"
    assert result["_action_event"] is True
    assert result["payload"]["confidence"] == 0.95


async def test_action_tool_elicit():
    """action tool emits elicit events."""
    from p8.api.tools.action import action

    result = await action(
        type="elicit",
        payload={"question": "Which format?", "options": ["A", "B"]},
    )
    assert result["action_type"] == "elicit"


# ---------------------------------------------------------------------------
# User profile resource
# ---------------------------------------------------------------------------


async def test_user_profile_resource(db, encryption, test_user):
    """user_profile resource loads a user by user_id."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.mcp_server import user_profile

    init_tools(db, encryption)
    set_tool_context(user_id=USER_ADA)

    result = await user_profile()
    data = json.loads(result)
    assert data["name"] == "Ada Lovelace"
    assert data["email"] == "ada@example.com"
    assert "computing" in data["tags"]


async def test_user_profile_not_found(db, encryption):
    """user_profile returns error when no user in context."""
    from p8.api.tools import init_tools, set_tool_context
    from p8.api.mcp_server import user_profile
    from uuid import uuid4

    init_tools(db, encryption)
    set_tool_context(user_id=uuid4())  # nonexistent user

    result = await user_profile()
    data = json.loads(result)
    assert "error" in data


# ---------------------------------------------------------------------------
# ask_agent — delegation
# ---------------------------------------------------------------------------


async def test_ask_agent_delegates(db, encryption):
    """ask_agent invokes another agent and returns its response."""
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="echo-agent",
        kind="agent",
        description="Echo agent",
        content="Repeat what the user says.",
    ))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="Echo: hello"))

    with patch.object(AgentAdapter, "build_agent", patched_build):
        result = await ask_agent("echo-agent", "hello")

    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert result["agent_schema"] == "echo-agent"
    assert "Echo: hello" in result["text_response"]


async def test_ask_agent_not_found(db, encryption):
    """ask_agent returns error for nonexistent agent."""
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    result = await ask_agent("nonexistent-agent", "hello")
    assert result["status"] == "error"
    assert "not found" in result["error"]


async def test_ask_agent_self_delegation(db, encryption):
    """An agent can delegate to itself via ask_agent."""
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter, register_sample_agent
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)
    await register_sample_agent(db, encryption)

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="Self-delegated"))

    with patch.object(AgentAdapter, "build_agent", patched_build):
        result = await ask_agent("sample-agent", "Tell me about yourself")

    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert "Self-delegated" in result["text_response"]


# ---------------------------------------------------------------------------
# ask_agent — event sink streaming
# ---------------------------------------------------------------------------


async def test_ask_agent_streams_to_event_sink(db, encryption):
    """ask_agent pushes child_content events to event sink when available."""
    import asyncio
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.delegate import set_child_event_sink
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="event-test-agent",
        kind="agent",
        description="Agent for event testing",
        content="You respond with helpful information.",
    ))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="Child says hi"))

    sink: asyncio.Queue = asyncio.Queue()
    previous = set_child_event_sink(sink)

    try:
        with patch.object(AgentAdapter, "build_agent", patched_build):
            result = await ask_agent("event-test-agent", "hello")

        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["agent_schema"] == "event-test-agent"
        assert "Child says hi" in result["text_response"]

        events = []
        while not sink.empty():
            events.append(sink.get_nowait())

        assert len(events) >= 1
        content_events = [e for e in events if e["type"] == "child_content"]
        assert len(content_events) >= 1
        assert content_events[0]["agent_name"] == "event-test-agent"
    finally:
        set_child_event_sink(previous)


async def test_ask_agent_event_sink_content(db, encryption):
    """Child content events contain agent_name and text content."""
    import asyncio
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.delegate import set_child_event_sink
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="content-event-agent",
        kind="agent",
        description="Agent for content event testing",
        content="You answer questions.",
    ))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="The answer is 42"))

    sink: asyncio.Queue = asyncio.Queue()
    previous = set_child_event_sink(sink)

    try:
        with patch.object(AgentAdapter, "build_agent", patched_build):
            result = await ask_agent("content-event-agent", "What is the answer?")

        assert result["status"] == "success"

        events = []
        while not sink.empty():
            events.append(sink.get_nowait())

        content_events = [e for e in events if e["type"] == "child_content"]
        assert len(content_events) >= 1
        for e in content_events:
            assert e["agent_name"] == "content-event-agent"
        full_content = "".join(e["content"] for e in content_events)
        assert "42" in full_content
    finally:
        set_child_event_sink(previous)


async def test_ask_agent_error_returns_plain_dict(db, encryption):
    """ask_agent returns a plain dict on error."""
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    result = await ask_agent("nonexistent-for-event-test", "hello")

    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert "not found" in result["error"]


async def test_ask_agent_no_sink_uses_run(db, encryption):
    """Without event sink, ask_agent uses agent.run() (non-streaming)."""
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.delegate import get_child_event_sink
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="nosink-agent",
        kind="agent",
        description="Agent for no-sink test",
        content="You respond.",
    ))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="No sink response"))

    assert get_child_event_sink() is None

    with patch.object(AgentAdapter, "build_agent", patched_build):
        result = await ask_agent("nosink-agent", "hello")

    assert result["status"] == "success"
    assert "No sink response" in result["text_response"]


# ---------------------------------------------------------------------------
# Full agent build + run with MCP tools
# ---------------------------------------------------------------------------


async def test_build_agent_with_mcp_server(sample_adapter, mcp_server):
    """build_agent with mcp_server creates agent with toolsets."""
    agent = sample_adapter.build_agent(
        model_override=TestModel(custom_output_text="Answer from agent"),
        mcp_server=mcp_server,
    )
    assert agent is not None


async def test_agent_run_with_mcp_tools(sample_adapter, mcp_server):
    """Full agent run with TestModel and MCP toolset."""
    agent = sample_adapter.build_agent(
        model_override=TestModel(custom_output_text="I found the answer."),
        mcp_server=mcp_server,
    )

    result = await agent.run("What is in the knowledge base?")
    assert "I found the answer" in str(result.output)


async def test_agent_context_attributes(sample_adapter):
    """Context attributes include agent name and user info."""
    ctx = sample_adapter.build_context_attributes(
        user_id=USER_ADA,
        user_email="ada@example.com",
        session_id="sess-1",
    )
    msg = ctx.to_system_message()
    assert "Agent: sample-agent" in msg
    assert f"User ID: {USER_ADA}" in msg
    assert "ada@example.com" in msg


# ---------------------------------------------------------------------------
# Legacy schema format backward compat
# ---------------------------------------------------------------------------


async def test_legacy_schema_format_loads(db, encryption):
    """Old-format json_schema (model_name, response_schema) loads correctly."""
    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="legacy-agent",
        kind="agent",
        description="Legacy format agent",
        content="You are a legacy format agent.",
        json_schema={
            "model_name": "openai:gpt-4o",
            "temperature": 0.5,
            "tools": [
                {"name": "search", "server": "rem"},
            ],
            "response_schema": {
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
    ))

    adapter = await AgentAdapter.from_schema_name("legacy-agent", db, encryption)
    assert adapter.config.name == "legacy-agent"
    assert adapter.config.model == "openai:gpt-4o"
    assert adapter.config.temperature == 0.5
    assert len(adapter.config.tools) >= 1
    assert "answer" in adapter.config.properties


async def test_resources_merged_into_tools(db, encryption):
    """Old schemas with 'resources' get them merged into tools."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema._parse_dict({
        "type": "object",
        "name": "merge-test",
        "description": "Test",
        "tools": [{"name": "search"}],
        "resources": [{"name": "User Profile", "uri": "user://profile/{user_id}"}],
    })

    tool_names = {t.name for t in schema.tools}
    assert "search" in tool_names
    assert "user_profile" in tool_names
