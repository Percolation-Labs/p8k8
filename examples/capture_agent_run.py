"""Capture a full agent run with tool calls and delegation, then dump the DB message spread.

Simulates a realistic pydantic-ai agent run where the agent:
1. Calls `search` to query the knowledge base
2. Calls `ask_agent` to delegate to a structured output agent (analyzer)
3. Returns a final text response

The script constructs the pydantic-ai all_messages sequence (the same
structure pydantic-ai produces internally), persists it via persist_turn(),
reads back all message rows from the database, and saves them to YAML.

This demonstrates tool calls as first-class message rows:
  user → tool_call (search) → tool_call (ask_agent) → assistant

Usage:
    uv run python examples/capture_agent_run.py
"""

import asyncio
import json
from uuid import uuid4

import yaml
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from p8.agentic.adapter import AgentAdapter
from p8.api.tools import init_tools
from p8.ontology.types import Schema
from p8.services.repository import Repository
import p8.services.bootstrap as _svc


# Simulated tool results — what the tools would return in a real run
SEARCH_RESULT = json.dumps([
    {
        "name": "p8-architecture",
        "kind": "resource",
        "summary": "p8 is a minimal agentic framework where ontology is everything. "
                   "Every entity — models, agents, evaluators, tools — is a row in the "
                   "schemas table. Built on PostgreSQL 18, pgvector, pydantic-ai, FastAPI.",
        "topic_tags": ["architecture", "agents", "postgresql"],
    },
    {
        "name": "rem-query-system",
        "kind": "resource",
        "summary": "REM (Resource-Entity-Moment) query system supports LOOKUP, SEARCH, "
                   "FUZZY, TRAVERSE, and SQL modes. All backed by pgvector embeddings "
                   "and pg_trgm trigram indexes.",
        "topic_tags": ["rem", "search", "embeddings"],
    },
])

ANALYZER_RESULT = json.dumps({
    "status": "success",
    "output": {
        "summary": "p8 is a schema-driven agentic framework built on PostgreSQL where "
                   "every entity is a database row. It combines pgvector embeddings with "
                   "a multi-modal query system called REM.",
        "key_concepts": [
            "schema-driven agents",
            "ontology-as-database",
            "pgvector embeddings",
            "REM query system",
            "pydantic-ai integration",
        ],
        "confidence": 0.92,
    },
    "text_response": "p8 is a schema-driven agentic framework...",
    "agent_schema": "analyzer",
    "is_structured_output": True,
})


def build_all_messages(user_prompt: str) -> list:
    """Construct the pydantic-ai all_messages sequence for a multi-tool run.

    This mirrors what pydantic-ai produces internally when an agent:
    1. Receives user prompt
    2. Decides to call search
    3. Gets search results
    4. Decides to delegate to analyzer via ask_agent
    5. Gets structured output back
    6. Formulates final response
    """
    return [
        # 1. User prompt
        ModelRequest(parts=[
            UserPromptPart(content=user_prompt),
        ]),

        # 2. Model decides to search
        ModelResponse(parts=[
            ToolCallPart(
                tool_name="search",
                args={"query": 'SEARCH "p8 architecture" FROM resources LIMIT 3'},
                tool_call_id="call_search_001",
            ),
        ]),

        # 3. Search tool returns results
        ModelRequest(parts=[
            ToolReturnPart(
                tool_name="search",
                content=SEARCH_RESULT,
                tool_call_id="call_search_001",
            ),
        ]),

        # 4. Model delegates to analyzer agent
        ModelResponse(parts=[
            ToolCallPart(
                tool_name="ask_agent",
                args={
                    "agent_name": "analyzer",
                    "input_text": "Analyze the p8 architecture: minimal agentic framework, "
                                  "PostgreSQL 18, pgvector, pydantic-ai, REM query system.",
                },
                tool_call_id="call_ask_agent_001",
            ),
        ]),

        # 5. ask_agent returns structured output from analyzer
        ModelRequest(parts=[
            ToolReturnPart(
                tool_name="ask_agent",
                content=ANALYZER_RESULT,
                tool_call_id="call_ask_agent_001",
            ),
        ]),

        # 6. Final text response
        ModelResponse(parts=[
            TextPart(
                content="Based on my search and the analyzer's assessment, p8 is a "
                        "schema-driven agentic framework built on PostgreSQL 18. Its core "
                        "idea is that ontology IS the database — every agent, model, and "
                        "tool is a row in the schemas table. It uses pgvector for semantic "
                        "search and a query system called REM that supports LOOKUP, SEARCH, "
                        "FUZZY, and TRAVERSE modes. The analyzer identified 5 key concepts "
                        "with 0.92 confidence."
            ),
        ]),
    ]


def format_row(row: dict) -> dict:
    """Format a DB row for YAML output — keep only relevant columns.

    For tool_call rows, parses JSON content into structured data so
    the YAML output is human-readable (not a quoted JSON string).
    """
    out: dict = {
        "message_type": row["message_type"],
    }

    # Parse tool_calls JSONB
    if row.get("tool_calls") is not None:
        tc = row["tool_calls"]
        if isinstance(tc, str):
            tc = json.loads(tc)
        out["tool_calls"] = tc

    # Content — parse JSON strings into structured data for readability
    content = row.get("content")
    if content is not None:
        if row["message_type"] in ("tool_call", "tool_response") and content.startswith(("{", "[")):
            try:
                out["content"] = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                out["content"] = content
        else:
            out["content"] = content
    else:
        out["content"] = None

    # Usage metrics (assistant rows only)
    if row.get("input_tokens") and row["input_tokens"] > 0:
        out["input_tokens"] = row["input_tokens"]
    if row.get("output_tokens") and row["output_tokens"] > 0:
        out["output_tokens"] = row["output_tokens"]
    if row.get("latency_ms") and row["latency_ms"] > 0:
        out["latency_ms"] = row["latency_ms"]
    if row.get("model"):
        out["model"] = row["model"]
    if row.get("agent_name"):
        out["agent_name"] = row["agent_name"]
    return out


async def main():
    async with _svc.bootstrap_services() as (db, encryption, settings, *_rest):
        init_tools(db, encryption)
        repo = Repository(Schema, db, encryption)

        # Register the sample-agent (built-in)
        from p8.agentic.core_agents import SAMPLE_AGENT
        await repo.upsert(Schema(**SAMPLE_AGENT.to_schema_dict()))

        # Register the analyzer agent from .schema/
        analyzer_schema = Schema(
            name="analyzer",
            kind="agent",
            description="Structured output agent that analyzes text and returns key concepts.",
            content="You are a text analyzer. Given input text, extract a short summary "
                    "and a list of key concepts. Return structured JSON output.",
            json_schema={
                "structured_output": True,
                "response_schema": {
                    "properties": {
                        "summary": {"type": "string", "description": "A 1-2 sentence summary"},
                        "key_concepts": {"type": "array", "description": "3-5 key concepts"},
                        "confidence": {"type": "number", "description": "Confidence 0.0-1.0"},
                    },
                    "required": ["summary", "key_concepts"],
                },
                "model": "openai:gpt-4.1-mini",
                "temperature": 0.2,
            },
        )
        await repo.upsert(analyzer_schema)

        # Create a session
        from p8.ontology.types import Session
        session_id = uuid4()
        session = Session(
            id=session_id,
            name=f"example-agent-run-{session_id}",
            agent_name="sample-agent",
            mode="chat",
        )
        await Repository(Session, db, encryption).upsert(session)

        # Build adapter
        adapter = await AgentAdapter.from_schema_name("sample-agent", db, encryption)

        # Construct the simulated all_messages
        user_prompt = "Search for p8 architecture and ask the analyzer agent to summarize it"
        assistant_text = (
            "Based on my search and the analyzer's assessment, p8 is a "
            "schema-driven agentic framework built on PostgreSQL 18. Its core "
            "idea is that ontology IS the database — every agent, model, and "
            "tool is a row in the schemas table. It uses pgvector for semantic "
            "search and a query system called REM that supports LOOKUP, SEARCH, "
            "FUZZY, and TRAVERSE modes. The analyzer identified 5 key concepts "
            "with 0.92 confidence."
        )
        all_messages = build_all_messages(user_prompt)

        # Persist via the new persist_turn (extracts tool calls, saves as rows)
        await adapter.persist_turn(
            session_id,
            user_prompt,
            assistant_text,
            all_messages=all_messages,
            input_tokens=1250,
            output_tokens=180,
            latency_ms=2340,
            model="openai:gpt-4.1",
            agent_name="sample-agent",
        )

        # Read back from DB
        rows = await db.fetch(
            "SELECT message_type, content, tool_calls, token_count, "
            "       input_tokens, output_tokens, latency_ms, model, agent_name "
            "FROM messages WHERE session_id = $1 ORDER BY created_at",
            session_id,
        )

        messages = [format_row(dict(r)) for r in rows]

        # Build output
        output = {
            "description": (
                "Message spread from a single agent turn with tool calls and delegation.\n"
                "The agent called search (REM query), then delegated to the analyzer agent\n"
                "(structured output) via ask_agent.\n\n"
                "Sequence: user → tool_call → tool_response → tool_call → tool_response → assistant\n\n"
                "tool_call rows store call metadata (name, args, id) in tool_calls JSONB.\n"
                "tool_response rows store the tool result in content. For ask_agent, this\n"
                "captures the structured output artifact from the child agent.\n\n"
                "Generated by: uv run python examples/capture_agent_run.py"
            ),
            "session_id": str(session_id),
            "agent": "sample-agent",
            "message_count": len(messages),
            "messages": messages,
        }

        # Save YAML
        path = "examples/data/agent_run.yaml"
        header = (
            f"# {'=' * 70}\n"
            f"# AGENT RUN — Message spread from DB after persist_turn()\n"
            f"# {'=' * 70}\n"
            f"#\n"
            f"# Shows tool calls and responses as first-class message rows:\n"
            f"#   user → tool_call → tool_response → ... → assistant\n"
            f"#\n"
            f"# tool_call:     call metadata in tool_calls JSONB, content is NULL\n"
            f"# tool_response: tool result in content, correlation in tool_calls JSONB\n"
            f"#\n"
            f"# To regenerate: uv run python examples/capture_agent_run.py\n"
            f"# {'=' * 70}\n\n"
        )
        yaml_content = yaml.dump(
            output, default_flow_style=False, sort_keys=False,
            allow_unicode=True, width=120,
        )
        with open(path, "w") as f:
            f.write(header)
            f.write(yaml_content)

        print(f"Saved {path}")
        print(f"  session_id: {session_id}")
        print(f"  messages: {len(messages)}")
        print()
        for i, m in enumerate(messages):
            tc_info = ""
            if m.get("tool_calls"):
                tc = m["tool_calls"]
                tc_info = f"  tool={tc.get('name', '?')}"
            content_preview = ""
            if m.get("content"):
                c = str(m["content"])
                content_preview = f"  {c[:60]}..."
            print(f"  [{i}] {m['message_type']}{tc_info}{content_preview}")

        # Clean up
        await db.execute("DELETE FROM messages WHERE session_id = $1", session_id)
        await db.execute("DELETE FROM sessions WHERE id = $1", session_id)
        print("\nCleaned up test data.")


if __name__ == "__main__":
    asyncio.run(main())
