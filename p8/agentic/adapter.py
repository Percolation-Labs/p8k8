"""AgentAdapter — wraps a schema row (kind='agent') into a runnable pydantic-ai Agent.

Agents are declarative: YAML/JSON documents in the schemas table (kind='agent').
The ``json_schema`` column holds a flat AgentSchema dict (system prompt in
``description``, thinking aides in ``properties``, config fields like ``tools``,
``model``, ``limits`` at the top level). Adapters are cached with a 5-minute TTL.

Loading priority:
1. Database (Schema row with kind='agent')
2. Built-in code agents (core_agents.py)
3. YAML files from settings.schema_dir
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

from pydantic_ai.toolsets.fastmcp import FastMCPToolset

from p8.agentic.agent_schema import AgentSchema
from p8.agentic.core_agents import (
    BUILTIN_AGENT_DEFINITIONS,
    DREAMING_AGENT,
    GENERAL_AGENT,
    SAMPLE_AGENT,
)
from p8.agentic.types import ContextAttributes, ContextInjector, RoutingState
from p8.ontology.types import Moment, Schema
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.memory import MemoryService
from p8.services.repository import Repository


DEFAULT_AGENT_NAME = "general"

# Registry of code-defined agents as dicts, ready for Schema(**d).
# Populated from the AgentSchema instances in core_agents.py.
BUILTIN_AGENTS: dict[str, dict[str, Any]] = {
    name: defn.to_schema_dict()
    for name, defn in BUILTIN_AGENT_DEFINITIONS.items()
}


async def register_sample_agent(db: Database, encryption: EncryptionService) -> Schema:
    """Register the sample agent in the DB. Used by tests and bootstrapping."""
    repo = Repository(Schema, db, encryption)
    [result] = await repo.upsert(Schema(**SAMPLE_AGENT.to_schema_dict()))
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

    from p8.settings import get_settings

    settings = get_settings()
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

            if not isinstance(raw, dict):
                log.warning("Skipping %s: not a dict", filepath)
                continue

            # Support two YAML formats:
            # 1. Schema entity: {name, kind, content, json_schema}
            # 2. AgentSchema flat: {type: object, description, name, tools, ...}
            if "name" not in raw:
                # Check if it's flat AgentSchema format with name in nested
                # json_schema_extra (shouldn't happen, but handle gracefully)
                log.warning("Skipping %s: missing 'name' key", filepath)
                continue

            name = raw["name"]

            # If this looks like a flat AgentSchema (has 'description' and 'type'),
            # convert to Schema entity format for BUILTIN_AGENTS
            if raw.get("type") == "object" and "description" in raw:
                try:
                    schema = AgentSchema._parse_dict(raw)
                    raw = schema.to_schema_dict()
                except Exception as e:
                    log.warning("Failed to parse AgentSchema from %s: %s", filepath, e)
                    continue
            else:
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
    """Wraps a Schema row into a runnable pydantic-ai Agent.

    The adapter parses the Schema row's ``json_schema`` into an
    ``AgentSchema`` instance, which provides:
    - ``get_system_prompt()`` — system prompt + optional prompt guidance
    - ``get_options()`` — model/temperature/settings for pydantic-ai Agent
    - ``to_output_schema()`` — Pydantic model for structured output
    - ``tools`` / ``resources`` / ``limits`` — runtime config
    """

    def __init__(self, schema: Schema, db: Database, encryption: EncryptionService):
        self.schema = schema
        self.db = db
        self.encryption = encryption
        self.memory = MemoryService(db, encryption)
        self.agent_schema = AgentSchema.from_schema_row(schema)

    @property
    def config(self) -> AgentSchema:
        """Agent configuration (alias for agent_schema)."""
        return self.agent_schema

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
    # Tool resolution
    # ------------------------------------------------------------------

    def _get_delegate_tools(self) -> list:
        """Get delegate tool functions declared in the schema.

        Delegate tools (e.g. ask_agent) are registered as direct Python
        functions rather than loaded from MCP servers. This avoids
        namespace conflicts when the same tool is also on the MCP server.
        """
        tool_names = {t.name for t in self.agent_schema.tools}
        tools = []
        if "ask_agent" in tool_names:
            # Inline import to avoid circular: ask_agent imports AgentAdapter
            from p8.api.tools.ask_agent import ask_agent
            tools.append(ask_agent)
        return tools

    def _get_mcp_tool_names(self) -> set[str]:
        """Get tool names that should be loaded from MCP (excluding delegates)."""
        return {
            t.name for t in self.agent_schema.tools
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
        for ref in self.agent_schema.tools:
            if ref.name in DELEGATE_TOOL_NAMES:
                continue
            srv = ref.server or "local"
            tools_by_server.setdefault(srv, set()).add(ref.name)

        # Resolve local/rem server tools via FastMCPToolset
        local_servers = {"local", "rem"}
        for server_name, tool_names in tools_by_server.items():
            if server_name in local_servers:
                server: Any = mcp_server
                if server is None:
                    from p8.api.mcp_server import get_mcp_server
                    server = get_mcp_server()
                if server is None and mcp_url:
                    server = mcp_url

                if server is not None:
                    toolset: Any = FastMCPToolset(server)
                    if tool_names:
                        _filter = lambda ctx, td, allowed=tool_names: td.name in allowed  # type: ignore[misc]
                        toolset = toolset.filtered(_filter)
                    toolsets.append(toolset)
            elif mcp_url:
                # Remote server — use URL endpoint
                toolset_r: Any = FastMCPToolset(mcp_url)
                if tool_names:
                    _filter_r = lambda ctx, td, allowed=tool_names: td.name in allowed  # type: ignore[misc]
                    toolset_r = toolset_r.filtered(_filter_r)
                toolsets.append(toolset_r)

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
        """Construct a pydantic-ai Agent from the AgentSchema.

        Uses AgentSchema methods for all config resolution:
        - get_options() → model, model_settings
        - get_system_prompt() → system prompt + prompt guidance
        - to_output_schema() → structured output Pydantic model or str
        """
        # Get options from schema (model, model_settings)
        overrides = {}
        if model_override is not None:
            overrides["model"] = model_override
        options = self.agent_schema.get_options(**overrides)

        output_type = self.agent_schema.to_output_schema()

        toolsets, tools = self.resolve_toolsets(mcp_server=mcp_server, mcp_url=mcp_url)
        if extra_tools:
            tools.extend(extra_tools)
        if extra_toolsets:
            toolsets.extend(extra_toolsets)

        kwargs: dict[str, Any] = {
            **options,
            "system_prompt": self.agent_schema.get_system_prompt(),
            "name": self.agent_schema.name,
        }
        if output_type is not str:
            kwargs["output_type"] = output_type
        if tools:
            kwargs["tools"] = tools
        if toolsets:
            kwargs["toolsets"] = toolsets

        from p8.settings import get_settings
        s = get_settings()
        if s.otel_enabled:
            try:
                from pydantic_ai.models.instrumented import InstrumentationSettings
                kwargs["instrument"] = InstrumentationSettings(event_mode="logs")
            except ImportError:
                pass

        agent = Agent(**kwargs)
        if self.agent_schema.limits:
            agent._p8_usage_limits = self.agent_schema.limits.to_pydantic_ai()  # type: ignore[attr-defined]
        return agent

    # ------------------------------------------------------------------
    # Message history conversion
    # ------------------------------------------------------------------

    async def load_history(
        self,
        session_id: UUID,
        *,
        user_id: UUID | None = None,
        tenant_id: str | None = None,
        max_tokens: int | None = 8000,
        moment_limit: int = 3,
    ) -> list[ModelMessage]:
        """Load conversation history as pydantic-ai ModelMessages.

        Tries serialized pai_messages from session metadata first;
        falls back to reconstructing from DB rows via MemoryService.

        When tenant_id is provided, encrypted message content and moment
        summaries are decrypted using the tenant's DEK before being sent
        to the LLM.
        """
        messages = await self._load_pai_messages(session_id)
        if messages is not None:
            moments = await self._load_session_moments(
                session_id, limit=moment_limit, tenant_id=tenant_id,
            )
            return moments + messages

        raw = await self.memory.load_context(
            session_id, max_tokens=max_tokens, tenant_id=tenant_id,
        )
        return self._rows_to_model_messages(raw)

    async def _load_pai_messages(self, session_id: UUID) -> list[ModelMessage] | None:
        """Deserialize pydantic-ai messages from session metadata, or None."""
        log = logging.getLogger(__name__)
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
        except Exception as e:
            log.error("Failed to deserialize pai_messages for session %s: %s", session_id, e)
            raise

    async def _load_session_moments(
        self, session_id: UUID, *, limit: int = 3, tenant_id: str | None = None,
    ) -> list[ModelMessage]:
        """Load recent moments for a session as SystemPromptPart messages."""
        if tenant_id:
            await self.encryption.get_dek(tenant_id)
        rows = await self.db.fetch(
            "SELECT * FROM moments"
            " WHERE source_session_id = $1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT $2",
            session_id, limit,
        )
        messages: list[ModelMessage] = []
        for mrow in reversed(rows):
            md = self.encryption.decrypt_fields(Moment, dict(mrow), tenant_id)
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
                parts: list[TextPart | ToolCallPart] = [TextPart(content=content)]
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
        tenant_id: str | None = None,
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
        """Persist a conversation turn via rem_persist_turn().

        When tenant_id is provided, message content is encrypted with the
        tenant's DEK and encryption_level is stamped on the message rows.
        Sealed mode is capped to 'platform' for chat messages — the server
        must be able to decrypt history for the LLM.
        """
        from p8.ontology.types import Message

        pai_json: str | None = None
        if all_messages:
            # Filter out pure-system-prompt messages — they're regenerated
            # each turn by the ContextInjector and storing them causes the
            # pai_messages payload to grow unnecessarily.
            filtered = [
                msg for msg in all_messages
                if not (
                    isinstance(msg, ModelRequest)
                    and all(isinstance(p, SystemPromptPart) for p in msg.parts)
                )
            ]
            pai_json = ModelMessagesTypeAdapter.dump_json(filtered).decode()

        # Resolve encryption mode and encrypt content if tenant has encryption
        from uuid import uuid4

        encryption_level: str | None = None
        store_user = user_prompt
        store_assistant = assistant_text
        user_msg_id: UUID | None = None
        asst_msg_id: UUID | None = None
        if tenant_id:
            await self.encryption.get_dek(tenant_id)
            mode = await self.encryption.get_tenant_mode(tenant_id)
            # Sealed mode cannot work for chat — server must decrypt history
            # for LLM context. Cap to platform (server-side encryption).
            if mode == "sealed":
                mode = "platform"
            encryption_level = mode if mode != "disabled" else "disabled"
            if mode in ("platform", "client"):
                # Pre-generate IDs so encrypt_fields can bind AAD correctly
                user_msg_id = uuid4()
                asst_msg_id = uuid4()
                user_data = self.encryption.encrypt_fields(
                    Message, {"id": user_msg_id, "content": user_prompt}, tenant_id,
                )
                store_user = user_data.get("content", user_prompt)
                asst_data = self.encryption.encrypt_fields(
                    Message, {"id": asst_msg_id, "content": assistant_text}, tenant_id,
                )
                store_assistant = asst_data.get("content", assistant_text)

        await self.db.rem_persist_turn(
            session_id, store_user, store_assistant,
            user_id=user_id, tenant_id=tenant_id,
            tool_calls=tool_calls_data, pai_messages=pai_json,
            moment_threshold=moment_threshold if not background_compaction else 0,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms, model=model, agent_name=agent_name,
            encryption_level=encryption_level,
            user_msg_id=user_msg_id, asst_msg_id=asst_msg_id,
        )

        if background_compaction:
            import asyncio
            asyncio.create_task(
                self.memory.maybe_build_moment(
                    session_id, threshold=moment_threshold,
                    tenant_id=tenant_id, user_id=user_id,
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
