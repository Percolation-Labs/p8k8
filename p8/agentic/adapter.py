"""AgentAdapter — wraps a schema row (kind='agent') into a runnable pydantic-ai Agent.

Agents are declarative: YAML/JSON documents in the schemas table (kind='agent').
The content field holds the system prompt; json_schema holds runtime config
(model, tools, resources, limits). Adapters are cached with a 5-minute TTL.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings

from pydantic_ai.toolsets.fastmcp import FastMCPToolset

from p8.agentic.types import AgentConfig, ContextAttributes, ContextInjector, RoutingState
from p8.ontology.types import Moment, Schema
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService
from p8.services.repository import Repository


# ---------------------------------------------------------------------------
# Shared agent config — common tools/resources/settings for built-in agents
# ---------------------------------------------------------------------------

_REM_TOOLS = [
    {"name": "search", "server": "rem", "protocol": "mcp"},
    {"name": "action", "server": "rem", "protocol": "mcp"},
    {"name": "ask_agent", "server": "rem", "protocol": "mcp"},
    {"name": "remind_me", "server": "rem", "protocol": "mcp"},
]
_REM_RESOURCES = [{"uri": "user://profile/{user_id}", "name": "User Profile"}]


def _agent_config(
    *, max_tokens: int = 2000, request_limit: int = 10, token_limit: int = 50000,
) -> dict[str, Any]:
    """Build a standard agent json_schema dict (model resolved at runtime from settings)."""
    return {
        "model_name": "",
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "tools": _REM_TOOLS,
        "resources": _REM_RESOURCES,
        "limits": {"request_limit": request_limit, "total_tokens_limit": token_limit},
        "routing_enabled": True,
        "routing_max_turns": 20,
        "observation_mode": "sync",
    }


# ---------------------------------------------------------------------------
# Built-in agent definitions
# ---------------------------------------------------------------------------

SAMPLE_AGENT: dict[str, Any] = {
    "name": "sample-agent",
    "kind": "agent",
    "description": "Sample agent demonstrating the declarative schema structure.",
    "content": (
        "You are a helpful assistant with access to a knowledge base, "
        "user profiles, and the ability to delegate to other agents.\n\n"
        "Available capabilities:\n"
        "- search: Query the knowledge base using REM (LOOKUP, SEARCH, FUZZY, TRAVERSE, SQL)\n"
        "- action: Emit typed events (observation, elicit, delegate) for the UI\n"
        "- ask_agent: Delegate tasks to other specialist agents by name\n\n"
        "Always search the knowledge base before answering factual questions. "
        "Delegate to specialist agents when the task is outside your expertise."
    ),
    "json_schema": _agent_config(),
}

GENERAL_AGENT: dict[str, Any] = {
    "name": "general",
    "kind": "agent",
    "description": "Default REM-aware assistant with full knowledge base access.",
    "content": (
        "You are a knowledgeable assistant with access to a personal knowledge base "
        "powered by REM (Resource-Entity-Moment). You help users find, organize, and "
        "reason about their stored knowledge.\n\n"
        "## Your Tools\n\n"
        "### search\n"
        "Query the knowledge base using the REM dialect. Always search before answering "
        "factual questions about the user's data.\n\n"
        "**Query modes:**\n"
        "- `LOOKUP <key>` — Exact entity lookup by key\n"
        "- `SEARCH <text> FROM <table>` — Semantic vector search\n"
        "- `FUZZY <text>` — Fuzzy text matching across all entities\n"
        "- `TRAVERSE <key> DEPTH <n>` — Graph traversal from an entity\n"
        "- `SQL <query>` — Direct SQL (SELECT only)\n\n"
        "**Tables:** resources, moments, ontologies, files, sessions, users\n\n"
        "### action\n"
        "Emit structured events: `observation` (reasoning metadata) or `elicit` (clarification).\n\n"
        "### ask_agent\n"
        "Delegate to specialist agents for domain-specific tasks.\n\n"
        "### remind_me\n"
        "Create scheduled reminders that trigger push notifications.\n"
        "Use a cron expression for recurring (e.g. `0 9 * * 1` for every Monday at 9am) "
        "or an ISO datetime for one-time (e.g. `2025-03-01T09:00:00`).\n\n"
        "## Guidelines\n"
        "- Search before making claims about the user's data\n"
        "- When results are empty, try a broader query or different mode\n"
        "- Cite sources by referencing entity names from search results\n"
        "- Be concise but thorough"
    ),
    "json_schema": _agent_config(max_tokens=4000, request_limit=15, token_limit=80000),
}

_DREAMING_TOOLS = [
    {"name": "search", "server": "rem", "protocol": "mcp"},
    {"name": "save_moments", "server": "rem", "protocol": "mcp"},
]

DREAMING_AGENT: dict[str, Any] = {
    "name": "dreaming-agent",
    "kind": "agent",
    "description": "Background reflective agent that generates dream moments from recent user activity.",
    "content": (
        "You are a reflective dreaming agent. You and the person share a collaborative "
        "memory — you process recent conversations, moments, and resources together to "
        "surface insights and connections.\n\n"
        "## Voice\n\n"
        "Write in first-person plural: \"We discovered…\", \"We've been exploring…\", "
        "\"Our work on X connects to Y…\". Never say \"the user\" — this is a shared "
        "journal between you and the person.\n\n"
        "## Your Task\n\n"
        "You receive a summary of recent shared activity (moments, messages, resources).\n\n"
        "### Phase 1 — Reflect and draft dream moments\n"
        "Look across sessions for patterns, themes, and connections we might not have "
        "noticed in the moment.\n"
        "Draft 1-3 dream moments. Each dream moment has:\n"
        "- **name**: kebab-case identifier (e.g. `dream-ml-architecture-patterns`)\n"
        "- **summary**: 2-4 sentences capturing the insight, written in our shared voice\n"
        "- **topic_tags**: 3-5 relevant tags\n"
        "- **emotion_tags**: 0-2 emotional tones detected\n"
        "- **affinity_fragments**: links to entities from the context, each with:\n"
        "  - `target`: entity key (moment or resource name from the context)\n"
        "  - `relation`: relationship type (`thematic_link`, `builds_on`, `contrasts_with`, `elaborates`)\n"
        "  - `weight`: 0.0-1.0 strength\n"
        "  - `reason`: a short comment explaining why this connection matters\n\n"
        "### Phase 2 — Semantic search for deeper connections\n"
        "Propose 5 search questions based on the themes you found.\n"
        "For each question, call `search` twice:\n"
        "  - `SEARCH \"<question>\" FROM moments LIMIT 2`\n"
        "  - `SEARCH \"<question>\" FROM resources LIMIT 2`\n\n"
        "Add any newly discovered affinities to your dream moments.\n\n"
        "### Phase 3 — Save\n"
        "Call `save_moments` with your final collection of dream moments.\n"
        "Pass the full list as the `moments` parameter.\n\n"
        "## Guidelines\n"
        "- Focus on cross-session themes and emerging patterns\n"
        "- Surface connections we might not have noticed in the flow of conversation\n"
        "- Keep summaries concise but insightful, always in our shared voice\n"
        "- Every affinity_fragment must include a `reason` explaining the connection\n"
        "- Only reference entities that appear in the provided context or search results\n"
        "- Prefer depth over breadth: 1 insightful dream is better than 3 shallow ones\n"
        "- You MUST call save_moments at the end to persist your work"
    ),
    "json_schema": {
        "model_name": "openai:gpt-4.1-mini",
        "temperature": 0.7,
        "max_tokens": 4000,
        "structured_output": False,
        "tools": _DREAMING_TOOLS,
        "limits": {"request_limit": 15, "total_tokens_limit": 115000},
        "routing_enabled": False,
        "observation_mode": "disabled",
    },
}

DEFAULT_AGENT_NAME = "general"

# Registry of code-defined agents. Auto-registered on first DB miss.
BUILTIN_AGENTS: dict[str, dict[str, Any]] = {
    "sample-agent": SAMPLE_AGENT,
    "general": GENERAL_AGENT,
    "dreaming-agent": DREAMING_AGENT,
}


async def register_sample_agent(db: Database, encryption: EncryptionService) -> Schema:
    """Register the sample agent in the DB. Used by tests and bootstrapping."""
    repo = Repository(Schema, db, encryption)
    [result] = await repo.upsert(Schema(**SAMPLE_AGENT))
    return result

_yaml_loaded = False


def _load_yaml_agents() -> None:
    """Load agent definitions from YAML files in settings.schema_dir.

    Each YAML file should contain a dict with at least 'name' and 'kind'.
    Files are matched by *.yaml and *.yml. The folder may not exist or
    be empty — both are fine.

    Called once lazily on first cache miss. Merges into BUILTIN_AGENTS
    without overwriting code-defined entries.
    """
    global _yaml_loaded
    if _yaml_loaded:
        return
    _yaml_loaded = True

    from p8.settings import Settings

    settings = Settings()
    schema_path = Path(settings.schema_dir)

    if not schema_path.is_dir():
        return

    log = logging.getLogger(__name__)

    for ext in ("*.yaml", "*.yml"):
        for filepath in schema_path.glob(ext):
            try:
                raw = yaml.safe_load(filepath.read_text())
            except Exception as e:
                log.warning("Failed to parse %s: %s", filepath, e)
                continue

            if not isinstance(raw, dict) or "name" not in raw:
                log.warning("Skipping %s: missing 'name' key", filepath)
                continue

            name = raw["name"]
            # Default kind to 'agent' for schema_dir files
            raw.setdefault("kind", "agent")
            # Don't overwrite code-defined agents
            if name not in BUILTIN_AGENTS:
                BUILTIN_AGENTS[name] = raw
                log.debug("Loaded agent '%s' from %s", name, filepath)


async def _ensure_builtin(
    name: str, db: Database, encryption: EncryptionService,
) -> Schema | None:
    """If name matches a built-in or YAML agent, register it and return the Schema."""
    # Lazy-load YAML agents on first miss
    _load_yaml_agents()

    defn = BUILTIN_AGENTS.get(name)
    if defn is None:
        return None
    repo = Repository(Schema, db, encryption)
    schema = Schema(**defn)
    [result] = await repo.upsert(schema)
    return result


# ---------------------------------------------------------------------------
# TTL cache — avoids DB reload on every from_schema_name() call
# ---------------------------------------------------------------------------

_adapter_cache: dict[str, tuple[AgentAdapter, float]] = {}
_CACHE_TTL = 300  # 5 minutes


def _cache_key(name: str, user_id: UUID | None) -> str:
    return f"{name}:{str(user_id) if user_id else ''}"


# ---------------------------------------------------------------------------
# Tool names that are delegate tools (registered as direct functions,
# not loaded from MCP server to avoid namespace conflicts).
# ---------------------------------------------------------------------------

DELEGATE_TOOL_NAMES = {"ask_agent"}


# ---------------------------------------------------------------------------
# AgentAdapter
# ---------------------------------------------------------------------------


class AgentAdapter:
    """Wraps a Schema row into a runnable pydantic-ai Agent."""

    def __init__(self, schema: Schema, db: Database, encryption: EncryptionService):
        self.schema = schema
        self.db = db
        self.encryption = encryption
        self.memory = MemoryService(db, encryption)
        self.config = AgentConfig.from_json_schema(schema.json_schema)

    @classmethod
    async def from_schema_name(
        cls,
        name: str,
        db: Database,
        encryption: EncryptionService,
        *,
        user_id: UUID | None = None,
    ) -> AgentAdapter:
        """Load an agent schema by name from the database.

        If not found in DB, checks BUILTIN_AGENTS and auto-registers.
        Results are cached with a TTL to avoid DB reload on every call.
        """
        key = _cache_key(name, user_id)
        cached = _adapter_cache.get(key)
        if cached:
            adapter, ts = cached
            if time.monotonic() - ts < _CACHE_TTL:
                return adapter

        repo = Repository(Schema, db, encryption)
        results = await repo.find(filters={"name": name, "kind": "agent"}, limit=1)
        if not results:
            # Fall back to built-in agents — auto-register on first use
            builtin = await _ensure_builtin(name, db, encryption)
            if builtin is None:
                raise ValueError(f"Agent schema not found: {name}")
            results = [builtin]
        adapter = cls(results[0], db, encryption)
        _adapter_cache[key] = (adapter, time.monotonic())
        return adapter

    # ------------------------------------------------------------------
    # Config extraction
    # ------------------------------------------------------------------

    def _get_model_string(self) -> str:
        """Extract model string from config, falling back to settings.default_model."""
        model = self.config.model_name
        if not model:
            from p8.settings import Settings
            return Settings().default_model
        if ":" not in model:
            return f"openai:{model}"
        return model

    def _get_model_settings(self) -> ModelSettings | None:
        """Build ModelSettings (TypedDict) from config."""
        kwargs: dict[str, Any] = {}
        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            kwargs["max_tokens"] = self.config.max_tokens
        return ModelSettings(**kwargs) if kwargs else None

    def _get_system_prompt(self) -> str:
        """Return the system prompt — schema content, falling back to description.

        When structured output is disabled but response_schema properties
        are defined, appends human-readable field guidance to the prompt.
        """
        base = self.schema.content or self.schema.description or ""
        # Append prompt guidance when not using structured output
        if not self.config.structured_output:
            guidance = self.config.to_prompt_guidance()
            if guidance:
                base = f"{base}\n\n{guidance}"
        return base

    # ------------------------------------------------------------------
    # Tool resolution
    # ------------------------------------------------------------------

    def _get_delegate_tools(self) -> list:
        """Get delegate tool functions declared in the schema.

        Delegate tools (e.g. ask_agent) are registered as direct Python
        functions rather than loaded from MCP servers. This avoids
        namespace conflicts when the same tool is also on the MCP server.
        """
        tool_names = {t.name for t in self.config.tools}
        tools = []
        if "ask_agent" in tool_names:
            # Inline import to avoid circular: ask_agent imports AgentAdapter
            from p8.api.tools.ask_agent import ask_agent
            tools.append(ask_agent)
        return tools

    def _get_mcp_tool_names(self) -> set[str]:
        """Get tool names that should be loaded from MCP (excluding delegates)."""
        return {
            t.name for t in self.config.tools
            if t.name not in DELEGATE_TOOL_NAMES
        }

    def resolve_toolsets(
        self,
        *,
        mcp_server: Any | None = None,
        mcp_url: str | None = None,
    ) -> tuple[list, list]:
        """Resolve tool references to pydantic-ai toolsets and direct tools.

        Uses ``FastMCPToolset`` from pydantic-ai to load tools from the
        local FastMCP server (in-process) or a remote MCP endpoint.

        Groups tools by server, creates filtered toolsets for each,
        and separates out delegate tools (ask_agent) as plain functions.

        Args:
            mcp_server: Local FastMCP server instance for in-process tools.
                If None, falls back to the singleton from p8.api.mcp_server.
            mcp_url: Remote MCP endpoint URL. Used for tools whose server
                is not "local"/"rem", or as fallback when no local server.

        Returns:
            (toolsets, tools) — toolsets for Agent(toolsets=...),
            tools for Agent(tools=...).
        """
        toolsets: list = []
        tools: list = []

        # Group tools by server name
        tools_by_server: dict[str, set[str]] = {}
        for ref in self.config.tools:
            if ref.name in DELEGATE_TOOL_NAMES:
                continue
            server = ref.server or "local"
            tools_by_server.setdefault(server, set()).add(ref.name)

        # Resolve local/rem server tools via FastMCPToolset
        local_servers = {"local", "rem"}
        for server_name, tool_names in tools_by_server.items():
            if server_name in local_servers:
                server = mcp_server
                if server is None:
                    try:
                        from p8.api.mcp_server import get_mcp_server
                        server = get_mcp_server()
                    except Exception:
                        pass
                if server is None and mcp_url:
                    server = mcp_url

                if server is not None:
                    ts = FastMCPToolset(server)
                    if tool_names:
                        ts = ts.filtered(
                            lambda ctx, td, allowed=tool_names: td.name in allowed
                        )
                    toolsets.append(ts)
            elif mcp_url:
                # Remote server — use URL endpoint
                ts = FastMCPToolset(mcp_url)
                if tool_names:
                    ts = ts.filtered(
                        lambda ctx, td, allowed=tool_names: td.name in allowed
                    )
                toolsets.append(ts)

        # Delegate tools — registered as direct Python functions
        tools.extend(self._get_delegate_tools())

        return toolsets, tools

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def build_context_attributes(
        self,
        *,
        user_id: UUID | None = None,
        user_email: str | None = None,
        user_name: str | None = None,
        session_id: str | None = None,
        session_name: str | None = None,
        session_metadata: dict | None = None,
        routing_state: RoutingState | None = None,
    ) -> ContextAttributes:
        """Build context attributes for this agent and request."""
        return ContextAttributes(
            user_id=user_id,
            user_email=user_email,
            user_name=user_name,
            session_id=session_id,
            agent_name=self.schema.name,
            session_name=session_name,
            session_metadata=session_metadata,
            routing_table=routing_state.model_dump() if routing_state else {},
        )

    def build_injector(
        self,
        *,
        user_id: UUID | None = None,
        user_email: str | None = None,
        user_name: str | None = None,
        session_id: str | None = None,
        session_name: str | None = None,
        session_metadata: dict | None = None,
        routing_state: RoutingState | None = None,
        extra_sections: list[str] | None = None,
    ) -> ContextInjector:
        """Build a ContextInjector for this agent and request.

        The injector produces pydantic-ai ``instructions`` that inject
        context attributes (date, user, routing table) after the system
        prompt. Pass ``injector.instructions`` to ``agent.run()``,
        ``agent.iter()``, or ``AGUIAdapter.run_stream()``.
        """
        attrs = self.build_context_attributes(
            user_id=user_id,
            user_email=user_email,
            user_name=user_name,
            session_id=session_id,
            session_name=session_name,
            session_metadata=session_metadata,
            routing_state=routing_state,
        )
        return ContextInjector(attrs, extra_sections=extra_sections)

    # ------------------------------------------------------------------
    # Agent construction
    # ------------------------------------------------------------------

    def build_agent(
        self,
        *,
        model_override: Any = None,
        mcp_server: Any = None,
        mcp_url: str | None = None,
        extra_tools: list | None = None,
        extra_toolsets: list | None = None,
    ) -> Agent:
        """Construct a pydantic-ai Agent from the schema."""
        model = model_override if model_override is not None else self._get_model_string()
        output_type = self.config.to_output_model()

        toolsets, tools = self.resolve_toolsets(mcp_server=mcp_server, mcp_url=mcp_url)
        if extra_tools:
            tools.extend(extra_tools)
        if extra_toolsets:
            toolsets.extend(extra_toolsets)

        kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": self._get_system_prompt(),
            "name": self.schema.name,
        }
        if ms := self._get_model_settings():
            kwargs["model_settings"] = ms
        if output_type is not str:
            kwargs["output_type"] = output_type
        if tools:
            kwargs["tools"] = tools
        if toolsets:
            kwargs["toolsets"] = toolsets

        from p8.settings import Settings
        s = Settings()
        if s.otel_enabled:
            try:
                from pydantic_ai.models.instrumented import InstrumentationSettings
                kwargs["instrument"] = InstrumentationSettings(event_mode="logs")
            except ImportError:
                pass

        agent = Agent(**kwargs)
        if self.config.limits:
            agent._p8_usage_limits = self.config.limits.to_pydantic_ai()
        return agent

    # ------------------------------------------------------------------
    # Message history conversion
    # ------------------------------------------------------------------

    async def load_history(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
        max_tokens: int | None = 8000,
        moment_limit: int = 3,
    ) -> list[ModelMessage]:
        """Load conversation history as pydantic-ai ModelMessages.

        Tries serialized pai_messages from session metadata first;
        falls back to reconstructing from DB rows via MemoryService.
        """
        messages = await self._load_pai_messages(session_id)
        if messages is not None:
            moments = await self._load_session_moments(session_id, limit=moment_limit)
            return moments + messages

        raw = await self.memory.load_context(session_id, max_tokens=max_tokens)
        return self._rows_to_model_messages(raw)

    async def _load_pai_messages(self, session_id: UUID) -> list[ModelMessage] | None:
        """Deserialize pydantic-ai messages from session metadata, or None."""
        row = await self.db.fetchrow(
            "SELECT metadata FROM sessions WHERE id = $1 AND deleted_at IS NULL",
            session_id,
        )
        if not row:
            return None
        pai_raw = (row["metadata"] or {}).get("pai_messages")
        if not pai_raw:
            return None
        try:
            raw_bytes = pai_raw if isinstance(pai_raw, bytes) else pai_raw.encode()
            messages = ModelMessagesTypeAdapter.validate_json(raw_bytes)
            return messages or None
        except Exception:
            return None

    async def _load_session_moments(
        self, session_id: UUID, *, limit: int = 3,
    ) -> list[ModelMessage]:
        """Load recent moments for a session as SystemPromptPart messages."""
        rows = await self.db.fetch(
            "SELECT * FROM moments"
            " WHERE source_session_id = $1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT $2",
            session_id, limit,
        )
        messages: list[ModelMessage] = []
        for mrow in reversed(rows):
            md = self.encryption.decrypt_fields(Moment, dict(mrow), None)
            messages.append(
                ModelRequest(parts=[SystemPromptPart(
                    content=f"[Session context]\n{md.get('summary', '')}"
                )])
            )
        return messages

    def _rows_to_model_messages(self, rows: list[dict]) -> list[ModelMessage]:
        """Convert DB message rows to pydantic-ai ModelMessage list.

        Handles message_type values:
        - user → ModelRequest with UserPromptPart
        - system → ModelRequest with SystemPromptPart
        - assistant → ModelResponse with TextPart
        - tool_call → ModelRequest with ToolReturnPart (tool results sent to model)
        - tool_result → skipped (ephemeral, not persisted by default)
        - observation → ModelRequest with UserPromptPart (observations are context)
        - memory → ModelRequest with SystemPromptPart (injected memories)
        - think → skipped (internal reasoning)
        """
        messages: list[ModelMessage] = []
        for row in rows:
            mt = row.get("message_type", "user")
            content = row.get("content") or ""
            tool_calls = row.get("tool_calls")

            if mt == "user":
                messages.append(
                    ModelRequest(parts=[UserPromptPart(content=content)])
                )
            elif mt == "system":
                messages.append(
                    ModelRequest(parts=[SystemPromptPart(content=content)])
                )
            elif mt == "assistant":
                parts = [TextPart(content=content)]
                # If this assistant message had tool calls, include them
                if tool_calls and isinstance(tool_calls, dict):
                    for tc in tool_calls.get("calls", []):
                        parts.append(ToolCallPart(
                            tool_name=tc.get("name", ""),
                            args=tc.get("arguments", {}),
                            tool_call_id=tc.get("id"),
                        ))
                messages.append(ModelResponse(parts=parts))
            elif mt == "tool_call":
                # Tool result message — stored when we persist tool call results
                if tool_calls and isinstance(tool_calls, dict):
                    tool_name = tool_calls.get("name", "")
                    tool_call_id = tool_calls.get("id")
                    messages.append(
                        ModelRequest(parts=[ToolReturnPart(
                            tool_name=tool_name,
                            content=content,
                            tool_call_id=tool_call_id or "",
                        )])
                    )
            elif mt == "observation":
                messages.append(
                    ModelRequest(parts=[UserPromptPart(
                        content=f"[Observation] {content}"
                    )])
                )
            elif mt == "memory":
                messages.append(
                    ModelRequest(parts=[SystemPromptPart(content=content)])
                )
            # think, tool_result → skipped

        return messages

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def persist_turn(
        self,
        session_id: UUID,
        user_prompt: str,
        assistant_text: str,
        *,
        user_id: UUID | None = None,
        tool_calls_data: dict | None = None,
        all_messages: list[ModelMessage] | None = None,
        background_compaction: bool = True,
        moment_threshold: int = 6000,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        model: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Persist a conversation turn via rem_persist_turn()."""
        pai_json: str | None = None
        if all_messages:
            pai_json = ModelMessagesTypeAdapter.dump_json(all_messages).decode()

        await self.db.rem_persist_turn(
            session_id, user_prompt, assistant_text,
            user_id=user_id, tool_calls=tool_calls_data, pai_messages=pai_json,
            moment_threshold=moment_threshold if not background_compaction else 0,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms, model=model, agent_name=agent_name,
        )

        if background_compaction:
            import asyncio
            asyncio.create_task(
                self.memory.maybe_build_moment(
                    session_id, threshold=moment_threshold, user_id=user_id,
                )
            )

    async def persist_tool_call(
        self,
        session_id: UUID,
        tool_name: str,
        tool_call_id: str,
        arguments: dict,
        result: str,
        *,
        user_id: UUID | None = None,
    ) -> None:
        """Persist a tool call result as a message row.

        Tool call metadata goes in the tool_calls JSONB column.
        The result content goes in the content TEXT column.
        message_type = 'tool_call'.
        """
        await self.memory.persist_message(
            session_id, "tool_call", result,
            user_id=user_id,
            tool_calls={"name": tool_name, "id": tool_call_id, "arguments": arguments},
        )

    async def persist_observation(
        self,
        session_id: UUID,
        content: str,
        *,
        user_id: UUID | None = None,
    ) -> None:
        """Persist an observation message."""
        await self.memory.persist_message(
            session_id, "observation", content,
            user_id=user_id,
        )
