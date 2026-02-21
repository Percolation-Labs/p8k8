"""Core agent definitions as Pydantic model subclasses.

Each agent IS a Pydantic BaseModel where:
- The class **docstring** is the system prompt
- The **fields** define thinking aides (conversational) or output structure (structured)
- The **model_config["json_schema_extra"]** is the runtime config (tools, model, etc.)

Fields as Thinking Aides (Conversational Mode)
----------------------------------------------
In conversational mode (the default), fields are internal scaffolding that
guides the LLM's reasoning.  Each field description tells the model what
to observe and track.  The LLM outputs only conversational text.

Fields as Structured Output
---------------------------
When ``structured_output: true``, the model MUST return a JSON object
matching the schema.  Use for background processors (DreamingAgent)
where output maps to database entities.

Tools
-----
Tool references are ``{name, server, description}`` dicts:
- **name**: Tool function name on the MCP server
- **server**: Omit for local (defaults to local)
- **description**: Optional suffix giving agent-specific context for the tool
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from p8.agentic.agent_schema import AgentSchema


# ---------------------------------------------------------------------------
# Helper models for DreamingAgent structured output
# ---------------------------------------------------------------------------


class AffinityFragment(BaseModel):
    """A link to a related entity, stored as a graph edge."""

    target: str = Field(description="Entity key (moment or resource name from context)")
    relation: str = Field(
        default="thematic_link",
        description="thematic_link | builds_on | contrasts_with | elaborates",
    )
    weight: float = Field(default=1.0, description="Connection strength 0.0-1.0")
    reason: str = Field(description="Why this connection matters")


class DreamMoment(BaseModel):
    """A dream moment to persist — maps to a Moment entity + GraphEdges."""

    name: str = Field(description="kebab-case identifier (e.g. dream-ml-architecture-patterns)")
    summary: str = Field(description="2-4 sentences in shared voice capturing the insight")
    topic_tags: list[str] = Field(default_factory=list, description="3-5 relevant tags")
    emotion_tags: list[str] = Field(default_factory=list, description="0-2 emotional tones")
    affinity_fragments: list[AffinityFragment] = Field(
        default_factory=list,
        description="Links to related entities as graph edges",
    )


# ---------------------------------------------------------------------------
# General agent — default user-facing assistant
# ---------------------------------------------------------------------------


class GeneralAgent(BaseModel):
    """You are a friendly, sharp assistant with access to a personal knowledge base \
powered by REM (Resource-Entity-Moment). Keep responses short and conversational — \
no bullet points, numbered lists, or long explanations unless the user explicitly \
asks you to explain or elaborate. Answer like a helpful friend: direct, warm, \
and to the point.

## Style
- Keep it brief. One or two sentences is usually enough.
- Only use lists or detailed breakdowns when the user asks to explain something.
- Be warm and casual, not robotic or formal.
- Search before making claims about the user's data.
- When results are empty, try a broader query or different mode.
- Cite sources by referencing entity names from search results."""

    user_intent: str = Field(
        description="Classify: question, task, greeting, follow-up, clarification",
    )
    topic: str = Field(
        description="Primary topic or entity the user is asking about",
    )
    requires_search: bool = Field(
        description="Whether to search the knowledge base before responding",
    )
    search_strategy: str = Field(
        description="If search needed: LOOKUP <key>, SEARCH <text> FROM <table>, FUZZY <text>, TRAVERSE <key>, or SQL",
    )

    model_config = {"json_schema_extra": {
        "name": "general",
        "short_description": "Default REM-aware assistant with full knowledge base access.",
        "tools": [
            {
                "name": "search",
                "description": "Query knowledge base using REM dialect (LOOKUP, SEARCH, FUZZY, TRAVERSE, SQL). Tables: resources, moments, ontologies, files, sessions, users",
            },
            {
                "name": "action",
                "description": "Emit structured events: observation (reasoning metadata) or elicit (clarification)",
            },
            {
                "name": "ask_agent",
                "description": "Delegate to specialist agents for domain-specific tasks",
            },
            {
                "name": "remind_me",
                "description": "Create scheduled reminders — cron for recurring, ISO datetime for one-time. Infer schedule from context, don't ask for confirmation",
            },
            {
                "name": "user_profile",
                "description": "Load user profile for personalized responses",
            },
        ],
    }}


# ---------------------------------------------------------------------------
# Dreaming agent — background reflective processing
# ---------------------------------------------------------------------------


class DreamingAgent(BaseModel):
    """You are a reflective dreaming agent. You and the person share a collaborative \
memory — you process recent conversations, moments, and resources together to \
surface insights and connections.

## Voice

Write in first-person plural: "We discovered…", "We've been exploring…", \
"Our work on X connects to Y…". Never say "the user" — this is a shared \
journal between you and the person.

## Your Task

You receive a summary of recent shared activity (moments, messages, resources).

### Phase 1 — Reflect and draft dream moments
Look across sessions for patterns, themes, and connections we might not have \
noticed in the moment.

### Phase 2 — Semantic search for deeper connections
Propose 5 search questions based on the themes you found.
For each question, call `search` twice:
  - `SEARCH "<question>" FROM moments LIMIT 2`
  - `SEARCH "<question>" FROM resources LIMIT 2`

Add any newly discovered affinities to your dream moments.

### Phase 3 — Save
Call `save_moments` with your final collection of dream moments.
Pass the full list as the `moments` parameter.

## Guidelines
- Focus on cross-session themes and emerging patterns
- Surface connections we might not have noticed in the flow of conversation
- Keep summaries concise but insightful, always in our shared voice
- Every affinity_fragment must include a `reason` explaining the connection
- Only reference entities that appear in the provided context or search results
- Prefer depth over breadth: 1 insightful dream is better than 3 shallow ones
- You MUST call save_moments at the end to persist your work"""

    dream_moments: list[DreamMoment] = Field(
        default_factory=list,
        description="1-3 dream moments to extract and persist via save_moments",
    )
    search_questions: list[str] = Field(
        default_factory=list,
        description="5 semantic search questions derived from cross-session themes",
    )
    cross_session_themes: list[str] = Field(
        default_factory=list,
        description="Recurring patterns spanning multiple sessions — each a short phrase",
    )

    model_config = {"json_schema_extra": {
        "name": "dreaming-agent",
        "short_description": "Background reflective agent that generates dream moments from recent user activity.",
        "structured_output": True,
        "tools": [
            {
                "name": "search",
                "description": "Semantic search across moments and resources for cross-session connections",
            },
            {
                "name": "save_moments",
                "description": "Persist dream moments with affinity_fragments as graph edges",
            },
        ],
        "model": "openai:gpt-4.1-mini",
        "temperature": 0.7,
        "limits": {"request_limit": 15, "total_tokens_limit": 115000},
        "routing_enabled": False,
        "observation_mode": "disabled",
    }}


# ---------------------------------------------------------------------------
# Sample agent — minimal example for tests and docs
# ---------------------------------------------------------------------------


class SampleAgent(BaseModel):
    """You are a helpful assistant with access to a knowledge base \
and the ability to delegate to other agents.

Always search the knowledge base before answering factual questions. \
Delegate to specialist agents when the task is outside your expertise."""

    topic: str = Field(
        description="Primary topic of the user's question",
    )
    requires_search: bool = Field(
        description="Whether to search the knowledge base first",
    )

    model_config = {"json_schema_extra": {
        "name": "sample-agent",
        "short_description": "Sample agent demonstrating the declarative schema structure.",
        "tools": [
            {"name": "search", "description": "Query the knowledge base using REM"},
            {"name": "action", "description": "Emit observation or elicit events"},
            {"name": "ask_agent", "description": "Delegate to specialist agents"},
        ],
        "limits": {"request_limit": 10, "total_tokens_limit": 50000},
    }}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def agent_to_schema_dict(agent_cls: type[BaseModel]) -> dict:
    """Convert an agent Pydantic class to a dict for ``Schema(**d)``."""
    schema = AgentSchema.from_model_class(agent_cls)
    return schema.to_schema_dict()


def agent_to_agent_schema(agent_cls: type[BaseModel]) -> AgentSchema:
    """Convert an agent Pydantic class to an AgentSchema instance."""
    return AgentSchema.from_model_class(agent_cls)


# ---------------------------------------------------------------------------
# Registry — all built-in agent classes
# ---------------------------------------------------------------------------

BUILTIN_AGENT_CLASSES: dict[str, type[BaseModel]] = {
    "general": GeneralAgent,
    "dreaming-agent": DreamingAgent,
    "sample-agent": SampleAgent,
}

# Pre-built AgentSchema instances
GENERAL_AGENT = AgentSchema.from_model_class(GeneralAgent)
DREAMING_AGENT = AgentSchema.from_model_class(DreamingAgent)
SAMPLE_AGENT = AgentSchema.from_model_class(SampleAgent)

BUILTIN_AGENT_DEFINITIONS: dict[str, AgentSchema] = {
    "general": GENERAL_AGENT,
    "dreaming-agent": DREAMING_AGENT,
    "sample-agent": SAMPLE_AGENT,
}

# Pre-built schema dicts for DB registration
BUILTIN_AGENT_DICTS: dict[str, dict] = {
    name: defn.to_schema_dict()
    for name, defn in BUILTIN_AGENT_DEFINITIONS.items()
}
