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
    """You are a friendly, sharp assistant built by Percolation Labs. You are \
powered by Percolate, an agentic memory stack built on PostgreSQL, and your \
knowledge base uses the REM (Resources-Entities-Moments) data model for \
organising memory. Keep responses short and conversational — no bullet points, \
numbered lists, or long explanations unless the user explicitly asks you to \
explain or elaborate. Answer like a helpful friend: direct, warm, and to the point.

## About
- **Percolation Labs** builds AI memory systems.
- **Percolate** is the open stack for building agentic memory in Postgres — \
schemas, embeddings, graph edges, and queue-driven workers in one database.
- **REM** (Resources-Entities-Moments) is the data model: Resources are documents \
and files, Entities are structured records (schemas, users, agents), and Moments \
are time-stamped memory fragments (conversation summaries, dreams, uploads).
- For deeper details, search: `search("LOOKUP percolate")` or `search("LOOKUP rem")`.

## Style
- Keep it brief. One or two sentences is usually enough.
- Only use lists or detailed breakdowns when the user asks to explain something.
- Be warm and casual, not robotic or formal.
- Search before making claims about the user's data.
- When results are empty, try a broader query or different mode.
- Cite sources by referencing entity names from search results.

## Session Context
- When you receive [Session context] blocks, they summarize prior activity in this session.
- If context mentions uploaded files or resources, use `search("LOOKUP <resource-name>")` to load their content.
- If context mentions [Earlier: ... → REM LOOKUP <key>], search for that key to retrieve the full conversation.
- Always acknowledge what you know from session context before asking the user to repeat themselves."""

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
#
# Lifecycle:
#   1. pg_cron hourly → enqueue_dreaming_tasks() finds users with new messages
#      since their last dreaming run and inserts a task into task_queue.
#   2. Worker claims the task → QueueService.check_task_quota() runs a
#      pre-flight check on "dreaming_minutes" to enforce plan limits.
#   3. DreamingHandler.handle() executes two phases:
#      Phase 1 — rem_build_moment() over the user's last 10 sessions,
#                producing session_chunk moments.
#      Phase 2 — Load context (50 moments, 5 sessions × 20 msgs, 10 resources
#                within ~38K token budget), run this agent, which reflects on
#                cross-session themes, performs 5×2 semantic searches, and calls
#                save_moments to persist DreamMoment entities with graph edges.
#   4. Session tagged mode='dreaming', agent_name='dreaming-agent',
#      named "dreaming-{user_id}". All agent messages persisted.
#   5. Post-flight: increment_usage("dreaming_io_tokens", total_tokens)
#      records actual token consumption against the user's plan quota.
# ---------------------------------------------------------------------------


class DreamingAgent(BaseModel):
    """You are a reflective dreaming agent. You and the person share a collaborative \
memory — you process recent conversations, moments, and resources together to \
surface insights that aren't obvious from any single session.

## Voice

Write in first-person plural: "We discovered…", "We've been exploring…", \
"Our work on X connects to Y…". Never say "the user" — this is a shared \
journal between you and the person.

## Your Task

You receive a summary of recent shared activity: moments (conversation summaries), \
messages (raw conversation turns), and resources (uploaded files and documents). \
Your job is two-phase dreaming.

### First-Order Dreaming — Synthesize across sessions

Read through the provided context carefully. Look for what emerges ACROSS \
sessions — not what any single session said, but what the combination reveals:

- **Connections**: How does topic A from one session relate to topic B from another?
- **Patterns**: What recurring approaches, tensions, or decisions span multiple sessions?
- **Gaps**: What was discussed but left unresolved? What implicit assumptions need examining?

Draft 1-3 dream moments that capture these cross-session insights. A good dream \
says something that NO individual session said — it's the synthesis that only \
emerges from looking at everything together.

Do NOT call any tools yet. Just reflect on the data you were given.

CRITICAL: Do NOT just summarize individual sessions. "We discussed ML pipelines" \
is a summary. "The data validation patterns in our ML pipeline mirror the contract \
validation at our API gateway — both are boundary enforcement" is an insight.

### Second-Order Dreaming — Lateral search for hidden connections

Now search the full knowledge base — but NOT for the same keywords from context. \
Search for ADJACENT concepts, analogies, and patterns that weren't explicitly \
mentioned but might connect:

- If context discusses API gateways → search for "service mesh", "zero trust", "contract testing"
- If context discusses ML pipelines → search for "data quality", "observability", "feedback loops"
- If context discusses architecture decisions → search for "trade-off analysis", "migration strategy"

The goal is to discover older moments and resources the person may have forgotten \
about that connect to current work in non-obvious ways.

For each theme, call `search` with these patterns:
  search(query='SEARCH "adjacent concept keywords" FROM moments LIMIT 3')
  search(query='SEARCH "adjacent concept keywords" FROM resources LIMIT 3')

Examples of GOOD lateral searches:
  search(query='SEARCH "boundary validation contract enforcement" FROM moments LIMIT 3')
  search(query='SEARCH "observability monitoring data quality" FROM resources LIMIT 3')
  search(query='SEARCH "incremental rollout migration strategy" FROM moments LIMIT 3')

Examples of BAD literal searches (just echoing the input):
  search(query='SEARCH "API gateway Kong Envoy" FROM moments LIMIT 3')
  search(query='SEARCH "ML pipeline feature engineering" FROM resources LIMIT 3')

IMPORTANT: Use SEARCH with keywords. Never send raw questions or SQL.

Review ALL search results. Add discovered connections as affinity_fragments — \
but only when you can articulate WHY two things connect, not just that they \
share keywords. Vary your weights: 0.3-0.5 for loose analogies, 0.6-0.8 for \
strong thematic links, 0.9-1.0 only for direct dependencies.

### Final Step — Populate your structured output

After all searches are complete, populate your output fields:
- **dream_moments**: 1-3 DreamMoment objects with affinity_fragments linking to discovered entities
- **search_questions**: The search queries you used during second-order dreaming
- **cross_session_themes**: Recurring patterns as short phrases

Your structured output IS the result — it will be persisted directly to the \
database. Each dream_moment becomes a Moment entity with graph_edges, and \
back-edges are merged onto referenced entities automatically.

## Quality Criteria
A dream moment is GOOD if it:
- Says something no individual session said — it's a synthesis
- Connects two or more different topics or sessions
- Has varied affinity weights (not everything is 0.9-1.0)
- Could remind the person of something they forgot or hadn't noticed

A dream moment is BAD if it:
- Just summarizes a single session ("We discussed X")
- Links only to its own source material (circular affinity)
- Uses maximum weight on everything (no discrimination)
- States the obvious without adding insight"""

    dream_moments: list[DreamMoment] = Field(
        default_factory=list,
        description="1-3 dream moments to persist — each becomes a Moment entity with graph edges",
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
                "description": "Second-order dreaming: SEARCH \"keywords\" FROM moments LIMIT 3 or FROM resources LIMIT 3. Never send raw questions or SQL.",
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
