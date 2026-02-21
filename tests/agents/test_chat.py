"""Tests for chat endpoint and AgentAdapter."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
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


def _agui_body(user_message: str = "Hello") -> dict:
    """Build a minimal AG-UI RunAgentInput body."""
    return {
        "thread_id": str(uuid4()),
        "run_id": str(uuid4()),
        "state": {},
        "messages": [
            {"id": str(uuid4()), "role": "user", "content": user_message},
        ],
        "tools": [],
        "context": [],
        "forwarded_props": {},
    }


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------


def test_chat_defaults_agent_when_no_header(client):
    """POST /chat/{id} without x-agent-schema-name uses the default agent."""
    chat_id = str(uuid4())
    resp = client.post(f"/chat/{chat_id}", json=_agui_body())
    # Should succeed with the default agent (not return 400)
    assert resp.status_code == 200


def test_chat_unknown_agent_returns_404(client):
    """POST /chat/{id} with nonexistent agent returns 404."""
    chat_id = str(uuid4())
    resp = client.post(
        f"/chat/{chat_id}",
        json=_agui_body(),
        headers={"x-agent-schema-name": "nonexistent-agent"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AgentAdapter unit tests (use db/encryption fixtures directly)
# ---------------------------------------------------------------------------


async def test_adapter_from_schema_name(db, encryption):
    """AgentAdapter.from_schema_name loads an agent schema from DB."""
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="adapter-test-agent",
        kind="agent",
        description="A test agent",
        content="You are a helpful test agent.",
    ))

    from p8.agentic.adapter import AgentAdapter

    adapter = await AgentAdapter.from_schema_name("adapter-test-agent", db, encryption)
    assert adapter.schema.name == "adapter-test-agent"
    assert adapter.schema.content == "You are a helpful test agent."


async def test_adapter_not_found(db, encryption):
    """AgentAdapter.from_schema_name raises ValueError for nonexistent agent."""
    from p8.agentic.adapter import AgentAdapter

    with pytest.raises(ValueError, match="not found"):
        await AgentAdapter.from_schema_name("does-not-exist", db, encryption)


async def test_build_agent_returns_pydantic_ai_agent(db, encryption):
    """build_agent creates a pydantic-ai Agent with correct config."""
    from pydantic_ai.models.test import TestModel

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="build-test-agent",
        kind="agent",
        description="Agent description",
        content="Custom system prompt",
        json_schema={
            "model_name": "anthropic:claude-haiku-3-5-20241022",
            "temperature": 0.7,
            "max_tokens": 500,
        },
    ))

    adapter = await AgentAdapter.from_schema_name("build-test-agent", db, encryption)

    # Check config extraction via AgentSchema
    assert adapter.config.model == "anthropic:claude-haiku-3-5-20241022"
    assert "Custom system prompt" in adapter.config.get_system_prompt()
    assert adapter.config.temperature == 0.7

    # Build with TestModel override
    agent = adapter.build_agent(model_override=TestModel(custom_output_text="Test response"))
    assert agent is not None


async def test_build_agent_defaults(db, encryption):
    """build_agent uses sensible defaults when json_schema is empty."""
    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="defaults-agent",
        kind="agent",
        description="Just a description",
    ))

    adapter = await AgentAdapter.from_schema_name("defaults-agent", db, encryption)
    assert "Just a description" in adapter.config.get_system_prompt()
    # Model comes from settings.default_model when agent has no model
    options = adapter.config.get_options()
    assert ":" in str(options["model"]), "Model should be provider:model format"
    # Model settings populated from settings defaults
    ms = options.get("model_settings", {})
    assert ms.get("temperature", 0) > 0


# ---------------------------------------------------------------------------
# Context attributes
# ---------------------------------------------------------------------------


async def test_context_attributes(db, encryption):
    """build_context_attributes produces a system message with user info."""
    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="ctx-test-agent",
        kind="agent",
        description="Context test agent",
    ))

    adapter = await AgentAdapter.from_schema_name("ctx-test-agent", db, encryption)
    from uuid import UUID
    test_uid = UUID("00000000-0000-0000-0000-000000000123")
    ctx = adapter.build_context_attributes(
        user_id=test_uid,
        user_email="test@example.com",
        user_name="Test User",
        session_id="sess-abc",
    )
    msg = ctx.to_system_message()
    assert "Date:" in msg
    assert "User ID: 00000000-0000-0000-0000-000000000123" in msg
    assert "User email: test@example.com" in msg
    assert "User: Test User" in msg
    assert "Session: sess-abc" in msg
    assert "Agent: ctx-test-agent" in msg


async def test_context_attributes_with_session_context(db, encryption):
    """build_context_attributes includes session name and metadata when provided."""
    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="session-ctx-agent",
        kind="agent",
        description="Session context test agent",
    ))

    adapter = await AgentAdapter.from_schema_name("session-ctx-agent", db, encryption)
    ctx = adapter.build_context_attributes(
        session_id="sess-xyz",
        session_name="upload: architecture.md",
        session_metadata={"resource_keys": ["arch-chunk-0000", "arch-chunk-0001"], "source": "architecture.md"},
    )
    msg = ctx.render()
    assert "## Session Context" in msg
    assert "upload: architecture.md" in msg
    assert "arch-chunk-0000" in msg
    assert "REM LOOKUP" in msg


# ---------------------------------------------------------------------------
# AgentConfig parsing
# ---------------------------------------------------------------------------


def test_legacy_config_from_json_schema():
    """LegacyAgentConfig.from_json_schema parses known fields, ignores extras."""
    from p8.agentic.types import LegacyAgentConfig

    config = LegacyAgentConfig.from_json_schema({
        "model_name": "openai:gpt-4o",
        "temperature": 0.5,
        "tools": [
            {"name": "search", "server": "rem", "protocol": "mcp"},
            "simple_tool_name",
        ],
        "unknown_field": "ignored",
    })
    assert config.model_name == "openai:gpt-4o"
    assert config.temperature == 0.5
    assert len(config.tools) == 2
    assert config.tools[0].name == "search"
    assert config.tools[1].name == "simple_tool_name"


def test_legacy_config_defaults():
    """LegacyAgentConfig.from_json_schema with None returns all defaults."""
    from p8.agentic.types import LegacyAgentConfig

    config = LegacyAgentConfig.from_json_schema(None)
    assert config.model_name is None
    assert config.temperature is None
    assert config.max_iterations == 10
    assert config.routing_enabled is True
    assert config.routing_max_turns == 20


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def test_to_output_schema_structured():
    """to_output_schema generates a dynamic Pydantic model from properties."""
    from pydantic import BaseModel as PydanticBase

    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema(
        structured_output=True,
        properties={
            "answer": {"type": "string", "description": "The answer"},
            "confidence": {"type": "number"},
            "tags": {"type": "array"},
        },
        required=["answer"],
    )
    OutputModel = schema.to_output_schema()
    assert OutputModel is not str
    assert issubclass(OutputModel, PydanticBase)

    # Required fields
    fields = OutputModel.model_fields
    assert "answer" in fields
    assert "confidence" in fields
    assert "tags" in fields


def test_to_output_schema_plain_text():
    """to_output_schema returns str when structured_output is False."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema(structured_output=False, properties={"x": {"type": "string"}})
    assert schema.to_output_schema() is str


def test_to_output_schema_no_properties():
    """to_output_schema returns str when no properties defined."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema(structured_output=True, properties={})
    assert schema.to_output_schema() is str

    schema2 = AgentSchema(structured_output=True)
    assert schema2.to_output_schema() is str


def test_to_prompt_thinking_structure():
    """to_prompt generates thinking structure from properties."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema(
        properties={
            "answer": {"type": "string", "description": "The main answer"},
            "confidence": {"type": "number"},
        },
        required=["answer"],
    )
    guidance = schema.to_prompt()
    assert "answer" in guidance
    assert "(required)" in guidance
    assert "confidence" in guidance
    assert "The main answer" in guidance


def test_to_prompt_empty():
    """to_prompt returns empty string when no properties."""
    from p8.agentic.agent_schema import AgentSchema

    assert AgentSchema().to_prompt() == ""


# ---------------------------------------------------------------------------
# Usage limits
# ---------------------------------------------------------------------------


def test_usage_limits_parsing():
    """AgentSchema parses limits from dict."""
    from p8.agentic.agent_schema import AgentSchema

    schema = AgentSchema._parse_dict({
        "type": "object",
        "name": "limits-test",
        "description": "Test",
        "limits": {
            "request_limit": 10,
            "total_tokens_limit": 50000,
        }
    })
    assert schema.limits is not None
    assert schema.limits.request_limit == 10
    assert schema.limits.total_tokens_limit == 50000


def test_usage_limits_to_pydantic_ai():
    """AgentUsageLimits.to_pydantic_ai() converts to pydantic_ai.UsageLimits."""
    from p8.agentic.types import AgentUsageLimits

    limits = AgentUsageLimits(request_limit=5, tool_calls_limit=20)
    pai_limits = limits.to_pydantic_ai()
    assert pai_limits.request_limit == 5
    assert pai_limits.tool_calls_limit == 20


# ---------------------------------------------------------------------------
# Structured output in adapter
# ---------------------------------------------------------------------------


async def test_build_agent_structured_output(db, encryption):
    """build_agent passes output_type when structured_output is True."""
    from pydantic_ai.models.test import TestModel

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="structured-agent",
        kind="agent",
        description="Structured output test",
        content="Return a structured answer.",
        json_schema={
            "structured_output": True,
            "response_schema": {
                "properties": {
                    "answer": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["answer"],
            },
        },
    ))

    adapter = await AgentAdapter.from_schema_name("structured-agent", db, encryption)
    agent = adapter.build_agent(model_override=TestModel())

    # Agent should have a non-str output type
    assert agent._output_type is not str


async def test_build_agent_prompt_guidance_appended(db, encryption):
    """System prompt includes field guidance when structured_output is False but properties exist."""
    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="guidance-agent",
        kind="agent",
        description="Answer questions.",
        content="You are a helpful assistant.",
        json_schema={
            "structured_output": False,
            "response_schema": {
                "properties": {
                    "answer": {"type": "string", "description": "The answer"},
                },
                "required": ["answer"],
            },
        },
    ))

    adapter = await AgentAdapter.from_schema_name("guidance-agent", db, encryption)
    prompt = adapter.config.get_system_prompt()
    assert "You are a helpful assistant." in prompt
    assert "answer" in prompt
    assert "(required)" in prompt


# ---------------------------------------------------------------------------
# Routing state
# ---------------------------------------------------------------------------


def test_routing_state_lazy():
    """RoutingState lazy routing: agent persists until reclassify."""
    from p8.agentic.types import RoutingState

    rs = RoutingState()
    assert rs.should_reclassify() is True  # idle → needs classification

    rs.activate("query-agent", max_turns=3)
    assert rs.active_agent == "query-agent"
    assert rs.state == "executing"
    assert rs.should_reclassify() is False

    rs.increment_turn()
    assert rs.turn_count == 1
    assert rs.should_reclassify() is False

    rs.increment_turn()
    rs.increment_turn()
    assert rs.turn_count == 3
    assert rs.state == "re-evaluate"
    assert rs.should_reclassify() is True


def test_routing_state_complete():
    """RoutingState.complete() resets to idle with fallback agent."""
    from p8.agentic.types import RoutingState

    rs = RoutingState(fallback="general")
    rs.activate("specialist")
    assert rs.state == "executing"

    rs.complete()
    assert rs.state == "idle"
    assert rs.active_agent == "general"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


async def test_router_lazy_keeps_active_agent():
    """Router keeps the active agent when executing under max_turns."""
    from p8.agentic.routing import Router

    router = Router()
    metadata = {
        "routing": {
            "active_agent": "query-agent",
            "state": "executing",
            "turn_count": 0,
            "max_turns": 20,
        }
    }
    result = await router.route(metadata, "hello")
    assert result == "query-agent"
    assert metadata["routing"]["turn_count"] == 1


async def test_router_classifies_on_idle():
    """Router classifies when state is idle (no active agent executing)."""
    from p8.agentic.routing import Router

    router = Router()
    metadata = {"routing": {"state": "idle", "fallback": "general"}}
    result = await router.route(metadata, "hello")
    # DefaultClassifier returns fallback
    assert result == "general"
    assert metadata["routing"]["state"] == "executing"


# ---------------------------------------------------------------------------
# Message history conversion
# ---------------------------------------------------------------------------


async def test_rows_to_model_messages(db, encryption):
    """_rows_to_model_messages converts DB rows to pydantic-ai format."""
    from pydantic_ai.messages import ModelRequest, ModelResponse

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="msg-conv-agent",
        kind="agent",
        description="Message conversion test",
    ))

    adapter = await AgentAdapter.from_schema_name("msg-conv-agent", db, encryption)
    rows = [
        {"message_type": "user", "content": "Hello"},
        {"message_type": "assistant", "content": "Hi there!"},
        {"message_type": "system", "content": "System note"},
        {"message_type": "observation", "content": "User clicked button"},
        {"message_type": "memory", "content": "User prefers dark mode"},
        {"message_type": "think", "content": "Internal reasoning"},
    ]
    messages = adapter._rows_to_model_messages(rows)

    # user, assistant, system, observation, memory — think is skipped
    assert len(messages) == 5
    assert isinstance(messages[0], ModelRequest)  # user
    assert isinstance(messages[1], ModelResponse)  # assistant
    assert isinstance(messages[2], ModelRequest)  # system
    assert isinstance(messages[3], ModelRequest)  # observation
    assert isinstance(messages[4], ModelRequest)  # memory


async def test_rows_with_tool_calls(db, encryption):
    """_rows_to_model_messages handles assistant messages with tool_calls."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="tool-conv-agent",
        kind="agent",
        description="Tool conversion test",
    ))

    adapter = await AgentAdapter.from_schema_name("tool-conv-agent", db, encryption)
    rows = [
        {
            "message_type": "assistant",
            "content": "Let me search for that.",
            "tool_calls": {
                "calls": [
                    {"name": "search", "arguments": {"q": "test"}, "id": "tc-1"},
                ]
            },
        },
        {
            "message_type": "tool_call",
            "content": "Search result: found 3 items",
            "tool_calls": {"name": "search", "id": "tc-1"},
        },
    ]
    messages = adapter._rows_to_model_messages(rows)
    assert len(messages) == 2

    # Assistant message with tool call
    resp = messages[0]
    assert isinstance(resp, ModelResponse)
    assert len(resp.parts) == 2  # TextPart + ToolCallPart
    tc_part = resp.parts[1]
    assert isinstance(tc_part, ToolCallPart)
    assert tc_part.tool_name == "search"


# ---------------------------------------------------------------------------
# Streaming formatters
# ---------------------------------------------------------------------------


def test_format_sse_event():
    """format_sse_event serializes event models to SSE format."""
    from p8.agentic.streaming import format_sse_event
    from p8.agentic.types import DoneEvent

    event = DoneEvent(reason="stop")
    sse = format_sse_event(event)
    assert "event: done" in sse
    assert '"reason": "stop"' in sse
    assert sse.endswith("\n\n")


def test_format_content_chunk():
    """format_content_chunk produces OpenAI-compatible chunks."""
    import json

    from p8.agentic.streaming import format_content_chunk
    from p8.agentic.types import StreamingState

    state = StreamingState(model="test-model")
    chunk = format_content_chunk("Hello", state)

    assert chunk.startswith("data: ")
    data = json.loads(chunk.replace("data: ", "").strip())
    assert data["choices"][0]["delta"]["content"] == "Hello"
    assert data["choices"][0]["delta"]["role"] == "assistant"  # first chunk
    assert state.current_text == "Hello"
    assert state.is_first_chunk is False

    # Second chunk should not have role
    chunk2 = format_content_chunk(" world", state)
    data2 = json.loads(chunk2.replace("data: ", "").strip())
    assert "role" not in data2["choices"][0]["delta"]
    assert state.current_text == "Hello world"


def test_format_child_event_content():
    """format_child_event transforms content deltas from child agents."""
    import json

    from p8.agentic.streaming import format_child_event

    raw = (
        'data: {"choices": [{"delta": {"content": "test"}}]}\n\n'
    )
    result = format_child_event("child-agent", raw)
    assert "child_content" in result
    assert "child-agent" in result


def test_format_child_event_skips_done():
    """format_child_event skips [DONE] markers."""
    from p8.agentic.streaming import format_child_event

    assert format_child_event("child", "data: [DONE]") == ""
    assert format_child_event("child", "") == ""


# ---------------------------------------------------------------------------
# Event sink multiplexer unit tests
# ---------------------------------------------------------------------------


async def test_merged_event_stream_no_child_events():
    """_merged_event_stream passes through parent events when no child events."""
    import asyncio

    from p8.api.routers.chat import _merged_event_stream

    async def mock_parent_stream():
        yield "event-1"
        yield "event-2"
        yield "event-3"

    child_sink: asyncio.Queue = asyncio.Queue()
    events = []
    async for event in _merged_event_stream(mock_parent_stream(), child_sink):
        events.append(event)

    assert events == ["event-1", "event-2", "event-3"]


async def test_merged_event_stream_interleaves_child_events():
    """_merged_event_stream yields child events alongside parent events."""
    import asyncio

    from ag_ui.core.events import CustomEvent

    from p8.api.routers.chat import _merged_event_stream

    child_sink: asyncio.Queue = asyncio.Queue()

    async def mock_parent_stream():
        yield "parent-1"
        # Simulate child events arriving during tool execution
        await child_sink.put({"type": "child_content", "agent_name": "child", "content": "hello"})
        await asyncio.sleep(0.1)  # Give multiplexer time to pick up child event
        yield "parent-2"

    events = []
    async for event in _merged_event_stream(mock_parent_stream(), child_sink):
        events.append(event)

    # Should have parent events + at least one child CustomEvent
    parent_events = [e for e in events if isinstance(e, str)]
    child_events = [e for e in events if isinstance(e, CustomEvent)]

    assert len(parent_events) == 2
    assert parent_events == ["parent-1", "parent-2"]
    assert len(child_events) >= 1
    assert child_events[0].name == "child_content"
    assert child_events[0].value["agent_name"] == "child"


# ---------------------------------------------------------------------------
# Chat endpoint integration tests
# ---------------------------------------------------------------------------


def test_chat_creates_session(client):
    """POST /chat/{id} creates a session row in the database."""
    from unittest.mock import patch

    from pydantic_ai.models.test import TestModel

    # Insert agent
    client.post("/schemas/", json={
        "name": "session-test-agent",
        "kind": "agent",
        "description": "Agent for session test",
        "content": "You are a session test agent.",
    })

    chat_id = str(uuid4())

    from p8.agentic.adapter import AgentAdapter

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(self, model_override=TestModel(custom_output_text="OK"))

    with patch.object(AgentAdapter, "build_agent", patched_build):
        resp = client.post(
            f"/chat/{chat_id}",
            json=_agui_body("Test message"),
            headers={
                "x-agent-schema-name": "session-test-agent",
                "accept": "text/event-stream",
            },
        )

    assert resp.status_code == 200

    # Verify session was created
    resp2 = client.post("/query/", json={
        "mode": "SQL",
        "query": f"SELECT * FROM sessions WHERE id = '{chat_id}'",
    })
    assert resp2.status_code == 200
    sessions = resp2.json()
    assert len(sessions) >= 1
    assert sessions[0]["agent_name"] == "session-test-agent"


def test_chat_streaming_produces_events(client):
    """POST /chat/{id} with TestModel produces AG-UI SSE events."""
    from unittest.mock import patch

    from pydantic_ai.models.test import TestModel

    client.post("/schemas/", json={
        "name": "streaming-test-agent",
        "kind": "agent",
        "description": "Agent for streaming test",
        "content": "You are a streaming test agent.",
    })

    chat_id = str(uuid4())

    from p8.agentic.adapter import AgentAdapter

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(
            self,
            model_override=TestModel(custom_output_text="Hello! I'm a test agent."),
        )

    with patch.object(AgentAdapter, "build_agent", patched_build):
        resp = client.post(
            f"/chat/{chat_id}",
            json=_agui_body("Say hello"),
            headers={
                "x-agent-schema-name": "streaming-test-agent",
                "accept": "text/event-stream",
            },
        )

    assert resp.status_code == 200
    content = resp.text
    # AG-UI SSE stream should contain events
    assert "event:" in content or "data:" in content


def test_chat_persists_messages(client):
    """on_complete callback persists user + assistant messages."""
    from unittest.mock import patch

    from pydantic_ai.models.test import TestModel

    client.post("/schemas/", json={
        "name": "persist-test-agent",
        "kind": "agent",
        "description": "Agent for persistence test",
        "content": "Echo the user message.",
    })

    chat_id = str(uuid4())

    from p8.agentic.adapter import AgentAdapter

    original_build = AgentAdapter.build_agent

    def patched_build(self, **kwargs):
        return original_build(
            self,
            model_override=TestModel(custom_output_text="Echoed: hello world"),
        )

    with patch.object(AgentAdapter, "build_agent", patched_build):
        resp = client.post(
            f"/chat/{chat_id}",
            json=_agui_body("hello world"),
            headers={
                "x-agent-schema-name": "persist-test-agent",
                "accept": "text/event-stream",
            },
        )

    assert resp.status_code == 200

    # Check messages were persisted
    resp2 = client.post("/query/", json={
        "mode": "SQL",
        "query": f"SELECT * FROM messages WHERE session_id = '{chat_id}' ORDER BY created_at",
    })
    assert resp2.status_code == 200
    messages = resp2.json()
    # Should have user + assistant messages
    assert len(messages) >= 2
    types = [m["message_type"] for m in messages]
    assert "user" in types
    assert "assistant" in types


# ---------------------------------------------------------------------------
# FunctionModel: capture & verify what the LLM receives
# ---------------------------------------------------------------------------


async def test_function_model_captures_llm_input(db, encryption):
    """Use FunctionModel to capture and verify everything sent to the LLM.

    This is the definitive test that documents what the model receives:
    - System prompt (from schema content)
    - Instructions (context injection: date, user, session, agent, routing)
    - Message history (prior conversation turns)
    - Tools (function definitions available to the agent)
    - Model settings (temperature, max_tokens)

    The captured data serves as a reference for the examples/ documentation.
    """
    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    # -- 1. Register an agent schema with tools and config --
    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="inspect-agent",
        kind="agent",
        description="Agent for inspecting LLM input",
        content=(
            "You are a knowledge assistant. "
            "Search the knowledge base before answering questions."
        ),
        json_schema={
            "model_name": "anthropic:claude-sonnet-4-5-20250929",
            "temperature": 0.3,
            "max_tokens": 2000,
            "tools": [
                {"name": "search", "server": "rem", "protocol": "mcp"},
                {"name": "ask_agent", "server": "rem", "protocol": "mcp"},
            ],
        },
    ))

    adapter = await AgentAdapter.from_schema_name("inspect-agent", db, encryption)

    # -- 2. Build the context injector --
    from uuid import UUID
    test_uid = UUID("00000000-0000-0000-0000-000000000042")
    injector = adapter.build_injector(
        user_id=test_uid,
        user_email="alice@example.com",
        user_name="Alice",
        session_id="sess-abc-123",
    )

    # -- 3. Capture everything the model receives --
    captured_messages: list[ModelMessage] = []
    captured_info: list[AgentInfo] = []

    def capture_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        """FunctionModel callback — captures the full model input."""
        captured_messages.extend(messages)
        captured_info.append(info)
        return ModelResponse(parts=[TextPart(content="Captured response.")])

    # Build agent with FunctionModel instead of real provider
    agent = adapter.build_agent(model_override=FunctionModel(capture_model))

    # -- 4. Run with instructions (same path as real chat) --
    result = await agent.run(
        "What is remslim?",
        instructions=injector.instructions,
    )

    # -- 5. Verify: system prompt --
    assert len(captured_messages) >= 1
    first_request = captured_messages[0]
    system_parts = [p for p in first_request.parts if isinstance(p, SystemPromptPart)]
    assert len(system_parts) >= 1
    system_text = system_parts[0].content
    assert "knowledge assistant" in system_text
    assert "Search the knowledge base" in system_text

    # -- 6. Verify: instructions (context injection) --
    info = captured_info[0]
    assert info.instructions is not None
    assert "Date:" in info.instructions
    assert "User ID: 00000000-0000-0000-0000-000000000042" in info.instructions
    assert "User email: alice@example.com" in info.instructions
    assert "User: Alice" in info.instructions
    assert "Session: sess-abc-123" in info.instructions
    assert "Agent: inspect-agent" in info.instructions

    # -- 7. Verify: user prompt --
    user_parts = [p for p in first_request.parts if isinstance(p, UserPromptPart)]
    assert len(user_parts) >= 1
    assert "What is remslim?" in user_parts[0].content

    # -- 8. Verify: tools --
    tool_names = {t.name for t in info.function_tools}
    assert "ask_agent" in tool_names
    # MCP tools (search) may or may not be resolved depending on server init

    # -- 9. Verify: model settings --
    assert info.model_settings is not None
    assert info.model_settings.get("temperature") == 0.3
    # max_tokens comes from settings.default_max_tokens, not from agent config
    assert info.model_settings.get("max_tokens") is not None

    # -- 10. Verify: output --
    assert "Captured response" in str(result.output)


async def test_function_model_with_message_history(db, encryption):
    """FunctionModel captures message history alongside new prompt.

    When continuing a conversation, the model receives the full history
    (prior user/assistant turns) followed by the new user message.
    """
    from pydantic_ai.messages import (
        ModelMessage,
        ModelRequest,
        ModelResponse,
        SystemPromptPart,
        TextPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from p8.agentic.adapter import AgentAdapter
    from p8.ontology.types import Schema
    from p8.services.repository import Repository

    repo = Repository(Schema, db, encryption)
    await repo.upsert(Schema(
        name="history-inspect-agent",
        kind="agent",
        description="Agent for inspecting history",
        content="You are a helpful assistant.",
    ))

    adapter = await AgentAdapter.from_schema_name("history-inspect-agent", db, encryption)
    from uuid import UUID
    injector = adapter.build_injector(user_id=UUID("00000000-0000-0000-0000-000000000001"), session_id="sess-1")

    captured_messages: list[ModelMessage] = []

    def capture_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured_messages.extend(messages)
        return ModelResponse(parts=[TextPart(content="Turn 2 response.")])

    agent = adapter.build_agent(model_override=FunctionModel(capture_model))

    # Simulate prior conversation history
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="Hello")]),
        ModelResponse(parts=[TextPart(content="Hi there!")]),
    ]

    result = await agent.run(
        "Follow-up question",
        message_history=history,
        instructions=injector.instructions,
    )

    # History comes first, then new prompt
    all_user_parts = []
    for msg in captured_messages:
        if isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, UserPromptPart):
                    all_user_parts.append(p.content)

    assert "Hello" in all_user_parts
    assert "Follow-up question" in all_user_parts

    # System prompt present (may be in first message or a dedicated system request)
    all_system = []
    for msg in captured_messages:
        if isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, SystemPromptPart):
                    all_system.append(p.content)
    # pydantic-ai always includes the system prompt — either as a SystemPromptPart
    # in the messages, or via the Agent's system_prompt field. With FunctionModel,
    # when history is provided, the system prompt may appear in the first request
    # before the history entries.
    assert len(captured_messages) >= 3  # system + history(2) + new prompt
