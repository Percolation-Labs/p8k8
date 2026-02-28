"""Tests for the ResearcherAgent — direct execution and delegation via GeneralAgent."""

from __future__ import annotations

import json
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic_ai.models.test import TestModel

from p8.api.tools import init_tools, set_tool_context

USER_ADA = UUID("00000000-0000-0000-0000-00000000ada0")


@pytest.fixture(autouse=True)
async def _clean(clean_db):
    yield


@pytest_asyncio.fixture
async def _setup(db, encryption):
    """Initialize tools and set user context."""
    init_tools(db, encryption)
    set_tool_context(user_id=USER_ADA, session_id=uuid4())


@pytest_asyncio.fixture
async def _cleanup_plots(db):
    """Remove plot_collection moments after each test."""
    yield
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'plot_collection' AND user_id = $1",
        USER_ADA,
    )


# ---------------------------------------------------------------------------
# ResearcherAgent schema parsing
# ---------------------------------------------------------------------------


def test_researcher_schema_from_model_class():
    """from_model_class parses ResearcherAgent correctly."""
    from p8.agentic.agent_schema import AgentSchema
    from p8.agentic.core_agents import ResearcherAgent

    schema = AgentSchema.from_model_class(ResearcherAgent)
    assert schema.name == "researcher"
    assert schema.structured_output is False
    assert schema.temperature == 0.4
    # Thinking aide fields
    assert "research_goal" in schema.properties
    assert "diagram_type" in schema.properties
    assert "requires_web_search" in schema.properties
    # Tools
    tool_names = {t.name for t in schema.tools}
    assert tool_names == {"search", "web_search", "save_plot"}
    # Limits
    assert schema.limits is not None
    assert schema.limits.request_limit == 20
    assert schema.limits.total_tokens_limit == 80000


def test_researcher_in_builtin_registries():
    """ResearcherAgent appears in all three registries."""
    from p8.agentic.core_agents import (
        BUILTIN_AGENT_CLASSES,
        BUILTIN_AGENT_DEFINITIONS,
        BUILTIN_AGENT_DICTS,
        ResearcherAgent,
    )

    assert "researcher" in BUILTIN_AGENT_CLASSES
    assert BUILTIN_AGENT_CLASSES["researcher"] is ResearcherAgent
    assert "researcher" in BUILTIN_AGENT_DEFINITIONS
    assert "researcher" in BUILTIN_AGENT_DICTS
    d = BUILTIN_AGENT_DICTS["researcher"]
    assert d["name"] == "researcher"
    assert d["kind"] == "agent"


def test_researcher_system_prompt_content():
    """Researcher prompt includes Mermaid reference and workflow instructions."""
    from p8.agentic.core_agents import BUILTIN_AGENT_DEFINITIONS

    schema = BUILTIN_AGENT_DEFINITIONS["researcher"]
    prompt = schema.get_system_prompt()
    assert "Mermaid" in prompt
    assert "save_plot" in prompt
    assert "moment_link" in prompt
    assert "graph LR" in prompt  # Flowchart quick-reference
    assert "sequenceDiagram" in prompt  # Sequence quick-reference
    assert "mindmap" in prompt  # Mindmap quick-reference
    assert "xychart-beta" in prompt  # Correct bar/line chart keyword
    assert "mermaid-syntax-reference" in prompt  # LOOKUP reference
    assert "[View diagram](moment://" in prompt  # Markdown link format


# ---------------------------------------------------------------------------
# GeneralAgent delegation prompt
# ---------------------------------------------------------------------------


def test_general_agent_has_delegation_section():
    """GeneralAgent system prompt includes delegation guidance for researcher."""
    from p8.agentic.core_agents import BUILTIN_AGENT_DEFINITIONS

    schema = BUILTIN_AGENT_DEFINITIONS["general"]
    prompt = schema.get_system_prompt()
    assert "## Delegation" in prompt
    assert "researcher" in prompt
    assert "ask_agent" in prompt
    assert "diagram" in prompt


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


async def test_save_plot_registered_on_mcp():
    """save_plot is registered as a tool on the MCP server."""
    from p8.api.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "save_plot" in tool_names


# ---------------------------------------------------------------------------
# save_plot returns moment_link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_plot_returns_moment_link(_setup, _cleanup_plots):
    """save_plot includes moment_link in its return dict."""
    from p8.api.tools.plots import save_plot

    result = await save_plot(
        title="Test Diagram",
        source="graph LR; A-->B;",
        plot_type="mermaid",
        topic_tags=["test"],
    )

    assert result["status"] == "success"
    assert "moment_link" in result
    assert result["moment_link"] == f"moment://{result['collection_name']}"
    assert result["moment_link"].startswith("moment://plots-")


# ---------------------------------------------------------------------------
# Direct researcher agent execution (TestModel)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_agent_direct(db, encryption, _setup, _cleanup_plots):
    """Researcher agent can be loaded and executed directly with TestModel."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.core_agents import BUILTIN_AGENT_DICTS
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    # Register researcher in DB
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**BUILTIN_AGENT_DICTS["researcher"]))

    # Load adapter
    adapter = await AgentAdapter.from_schema_name("researcher", db, encryption)
    assert adapter.agent_schema.name == "researcher"
    assert adapter.agent_schema.temperature == 0.4

    # Run with TestModel
    agent = adapter.build_agent(
        model_override=TestModel(
            custom_output_text="Here's a diagram of the API flow.\n\nmoment://plots-test-2026-02-27"
        ),
    )
    result = await agent.run("Research API gateway patterns and create a diagram")
    assert "diagram" in result.output.lower() or "moment://" in result.output


# ---------------------------------------------------------------------------
# Delegation: GeneralAgent → ResearcherAgent via ask_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_general_delegates_to_researcher(db, encryption, _setup, _cleanup_plots):
    """ask_agent can delegate to the researcher agent successfully."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.core_agents import BUILTIN_AGENT_DICTS
    from p8.api.tools.ask_agent import ask_agent
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    # Register researcher in DB
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**BUILTIN_AGENT_DICTS["researcher"]))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(
            self,
            model_override=TestModel(
                custom_output_text="I researched microservices patterns and created a diagram. View it at moment://plots-abc-2026-02-27"
            ),
        )

    with patch.object(AgentAdapter, "build_agent", patched_build):
        result = await ask_agent("researcher", "Research microservices patterns")

    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert result["agent_schema"] == "researcher"
    assert "moment://" in result["text_response"]


# ---------------------------------------------------------------------------
# Delegation with event sink streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_delegation_streams_events(db, encryption, _setup, _cleanup_plots):
    """Delegation to researcher pushes child_content events to event sink."""
    import asyncio

    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.core_agents import BUILTIN_AGENT_DICTS
    from p8.agentic.delegate import set_child_event_sink
    from p8.api.tools.ask_agent import ask_agent
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    # Register researcher in DB
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**BUILTIN_AGENT_DICTS["researcher"]))

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(
            self,
            model_override=TestModel(
                custom_output_text="Created a mindmap of ML concepts. moment://plots-abc-2026-02-27"
            ),
        )

    sink: asyncio.Queue = asyncio.Queue()
    previous = set_child_event_sink(sink)

    try:
        with patch.object(AgentAdapter, "build_agent", patched_build):
            result = await ask_agent("researcher", "Map out machine learning concepts")

        assert result["status"] == "success"
        assert result["agent_schema"] == "researcher"

        events = []
        while not sink.empty():
            events.append(sink.get_nowait())

        assert len(events) >= 1
        content_events = [e for e in events if e["type"] == "child_content"]
        assert len(content_events) >= 1
        assert content_events[0]["agent_name"] == "researcher"
        full_content = "".join(e["content"] for e in content_events)
        assert "moment://" in full_content
    finally:
        set_child_event_sink(previous)


# ---------------------------------------------------------------------------
# Researcher agent tools are resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_researcher_tools_resolved(db, encryption, _setup):
    """Researcher agent resolves search, web_search, save_plot tools from MCP."""
    from p8.agentic.adapter import AgentAdapter
    from p8.agentic.core_agents import BUILTIN_AGENT_DICTS
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(**BUILTIN_AGENT_DICTS["researcher"]))

    adapter = await AgentAdapter.from_schema_name("researcher", db, encryption)
    assert {t.name for t in adapter.agent_schema.tools} == {"search", "web_search", "save_plot"}
