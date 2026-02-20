"""Integration tests for AgentAdapter with MCP tool loading.

Tests the full agent construction pipeline:
- SampleAgent registration and loading
- Tool resolution from FastMCP server via FastMCPToolset
- Tools: search, action, ask_agent (delegate)
- Resource: user://profile/{user_id}
- Delegation (ask_agent calling another agent)
"""

from __future__ import annotations

import json
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
# SampleAgent registration & config
# ---------------------------------------------------------------------------


async def test_register_sample_agent(db, encryption):
    """register_sample_agent creates a schema row with correct config."""
    from p8.agentic.adapter import SAMPLE_AGENT, register_sample_agent

    schema = await register_sample_agent(db, encryption)
    assert schema.name == "sample-agent"
    assert schema.kind == "agent"
    assert "knowledge base" in schema.content
    assert schema.json_schema is not None
    assert schema.json_schema["temperature"] == 0.3
    assert len(schema.json_schema["tools"]) == 3


async def test_builtin_agent_auto_registers(db, encryption):
    """from_schema_name auto-registers built-in agents on first load."""
    from p8.agentic.adapter import AgentAdapter

    # No manual register_sample_agent — should auto-register from BUILTIN_AGENTS
    adapter = await AgentAdapter.from_schema_name("sample-agent", db, encryption)
    assert adapter.schema.name == "sample-agent"
    assert adapter.config.temperature == 0.3


# ---------------------------------------------------------------------------
# YAML file loader
# ---------------------------------------------------------------------------


def test_load_yaml_agents_from_dir(tmp_path):
    """_load_yaml_agents loads .yaml and .yml files from schema_dir."""
    import yaml

    from p8.agentic import adapter

    # Write two YAML agent files
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
    # Write a non-yaml file (should be ignored)
    (tmp_path / "notes.txt").write_text("not an agent")

    # Reset loader state and patch settings
    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import patch
        with patch("settings.Settings") as MockSettings:
            MockSettings.return_value.schema_dir = str(tmp_path)
            adapter._load_yaml_agents()

        assert "greeter" in adapter.BUILTIN_AGENTS
        assert "summarizer" in adapter.BUILTIN_AGENTS
        assert adapter.BUILTIN_AGENTS["greeter"]["content"] == "You greet people."
        assert adapter.BUILTIN_AGENTS["summarizer"]["json_schema"]["temperature"] == 0.2
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
        from unittest.mock import patch
        with patch("settings.Settings") as MockSettings:
            MockSettings.return_value.schema_dir = "/nonexistent/path"
            adapter._load_yaml_agents()

        # Should not crash, builtins unchanged
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
        from unittest.mock import patch
        with patch("settings.Settings") as MockSettings:
            MockSettings.return_value.schema_dir = str(tmp_path)
            adapter._load_yaml_agents()

        assert "valid-agent" in adapter.BUILTIN_AGENTS
        # bad.yaml should have been skipped
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


def test_load_yaml_does_not_overwrite_code_agents(tmp_path):
    """YAML agents don't overwrite code-defined agents."""
    import yaml

    from p8.agentic import adapter

    # Write a YAML file with the same name as the code-defined sample-agent
    (tmp_path / "sample-agent.yaml").write_text(yaml.dump({
        "name": "sample-agent",
        "kind": "agent",
        "content": "I am the impostor.",
    }))

    old_loaded = adapter._yaml_loaded
    old_builtins = dict(adapter.BUILTIN_AGENTS)
    try:
        adapter._yaml_loaded = False
        from unittest.mock import patch
        with patch("settings.Settings") as MockSettings:
            MockSettings.return_value.schema_dir = str(tmp_path)
            adapter._load_yaml_agents()

        # Code-defined version should win
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
        from unittest.mock import patch
        with patch("settings.Settings") as MockSettings:
            MockSettings.return_value.schema_dir = str(tmp_path)

            agent_adapter = await AgentAdapter.from_schema_name("yaml-bot", db, encryption)

        assert agent_adapter.schema.name == "yaml-bot"
        assert agent_adapter.config.temperature == 0.7
        assert "YAML file" in agent_adapter.schema.content
    finally:
        adapter._yaml_loaded = old_loaded
        adapter.BUILTIN_AGENTS.clear()
        adapter.BUILTIN_AGENTS.update(old_builtins)


async def test_sample_agent_config_parsing(sample_adapter):
    """SampleAgent config is parsed correctly into typed AgentConfig."""
    config = sample_adapter.config
    assert config.model_name == "anthropic:claude-sonnet-4-5-20250929"
    assert config.temperature == 0.3
    assert config.max_tokens == 2000
    assert len(config.tools) == 3
    assert {t.name for t in config.tools} == {"search", "action", "ask_agent"}
    assert len(config.resources) == 1
    assert config.resources[0].uri == "user://profile/{user_id}"
    assert config.limits is not None
    assert config.limits.request_limit == 10


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

    # Should have one toolset (for local MCP tools) and one delegate tool
    assert len(toolsets) == 1
    assert len(tools) == 1  # ask_agent


# ---------------------------------------------------------------------------
# FastMCPToolset — list and call tools
# ---------------------------------------------------------------------------


async def test_fastmcp_server_lists_tools(mcp_server):
    """FastMCP server exposes search, action, and ask_agent tools."""
    tools = await mcp_server.get_tools()
    tool_names = set(tools.keys())
    assert "search" in tool_names
    assert "action" in tool_names
    assert "ask_agent" in tool_names


async def test_fastmcp_toolset_creates_from_server(mcp_server):
    """FastMCPToolset can be instantiated from a FastMCP server."""
    from pydantic_ai.toolsets.fastmcp import FastMCPToolset

    toolset = FastMCPToolset(mcp_server)
    # Filtered toolset should also instantiate without error
    allowed = {"search", "action"}
    filtered = toolset.filtered(lambda ctx, td: td.name in allowed)
    assert filtered is not None


async def test_fastmcp_call_search(db, encryption, mcp_server):
    """Call search tool directly through FastMCP server."""
    # Seed something to search for
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
    from p8.api.tools import init_tools
    from p8.api.mcp_server import user_profile

    init_tools(db, encryption)

    result = await user_profile(str(USER_ADA))
    data = json.loads(result)
    assert data["name"] == "Ada Lovelace"
    assert data["email"] == "ada@example.com"
    assert "computing" in data["tags"]


async def test_user_profile_by_email(db, encryption, test_user):
    """user_profile resource falls back to email lookup."""
    from p8.api.tools import init_tools
    from p8.api.mcp_server import user_profile

    init_tools(db, encryption)

    result = await user_profile("ada@example.com")
    data = json.loads(result)
    assert data["name"] == "Ada Lovelace"


async def test_user_profile_not_found(db, encryption):
    """user_profile returns error for nonexistent user."""
    from p8.api.tools import init_tools
    from p8.api.mcp_server import user_profile

    init_tools(db, encryption)

    result = await user_profile("nonexistent-user")
    data = json.loads(result)
    assert "error" in data


# ---------------------------------------------------------------------------
# ask_agent — delegation
# ---------------------------------------------------------------------------


async def test_ask_agent_delegates(db, encryption):
    """ask_agent invokes another agent and returns its response (no event sink)."""
    from unittest.mock import patch

    from p8.agentic.adapter import AgentAdapter
    from p8.api.tools import init_tools
    from p8.api.tools.ask_agent import ask_agent

    init_tools(db, encryption)

    # Register a target agent
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

    # Without event sink, returns plain dict (agent.run fallback)
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

    # Set up event sink
    sink: asyncio.Queue = asyncio.Queue()
    previous = set_child_event_sink(sink)

    try:
        with patch.object(AgentAdapter, "build_agent", patched_build):
            result = await ask_agent("event-test-agent", "hello")

        # Returns a dict (not ToolReturn)
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["agent_schema"] == "event-test-agent"
        assert "Child says hi" in result["text_response"]

        # Event sink should have received child_content events
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
        # All content events should have agent_name
        for e in content_events:
            assert e["agent_name"] == "content-event-agent"
        # Concatenated content should contain the output
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

    # Ensure no event sink is set
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
