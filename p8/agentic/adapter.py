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
from p8.services.memory import MemoryService, format_moment_context
from p8.services.repository import Repository


log = logging.getLogger(__name__)

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

# Resource URI tools — tools backed by MCP resources instead of MCP tools.
# These are resolved separately from FastMCPToolset.
RESOURCE_SCHEME_MARKER = "://"


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

        # For built-in agents, propagate _source_output_model from code definition
        # (PrivateAttr is not serialized to DB, so we restore it here)
        builtin_def = BUILTIN_AGENT_DEFINITIONS.get(self.agent_schema.name)
        if builtin_def and builtin_def._source_output_model:
            self.agent_schema._source_output_model = builtin_def._source_output_model

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

        # For built-in agents, always sync code → DB so changes propagate
        builtin = await _ensure_builtin(name, db, encryption)
        if builtin is not None:
            results = [builtin]
        else:
            repo = Repository(Schema, db, encryption)
            results = await repo.find(filters={"name": name, "kind": "agent"}, limit=1)
            if not results:
                raise ValueError(f"Agent schema not found: {name}")
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
        """Get tool names that should be loaded from MCP (excluding delegates and resources)."""
        return {
            t.name for t in self.agent_schema.tools
            if t.name not in DELEGATE_TOOL_NAMES and not t.uri
        }

    def _get_resource_tools(self) -> list:
        """Create callable tools from resource URI references.

        Tools with a ``uri`` field are backed by MCP resources. Parametric
        templates (e.g. ``user://profile/{user_id}``) become tools whose
        parameters match the URI template variables.
        """
        import asyncio
        import inspect
        import re

        tools = []
        for ref in self.agent_schema.tools:
            if not ref.uri:
                continue

            uri_template = ref.uri
            tool_name = ref.name
            description = ref.description or f"Read resource: {uri_template}"
            params = re.findall(r"\{(\w+)\}", uri_template)

            if params:
                tool = _create_parameterized_resource_tool(
                    tool_name, uri_template, params, description,
                )
            else:
                tool = _create_static_resource_tool(
                    tool_name, uri_template, description,
                )
            tools.append(tool)

        return tools

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

        # Group tools by server name (skip delegates and resource-backed tools)
        tools_by_server: dict[str, set[str]] = {}
        for ref in self.agent_schema.tools:
            if ref.name in DELEGATE_TOOL_NAMES or ref.uri:
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

        # Resource tools — MCP resources exposed as callable tools
        tools.extend(self._get_resource_tools())

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
        """Load conversation history as pydantic-ai ModelMessages via MemoryService."""
        raw = await self.memory.load_context(
            session_id, max_tokens=max_tokens, tenant_id=tenant_id,
            max_moments=moment_limit,
        )
        return self._rows_to_model_messages(raw)

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
                    content=format_moment_context(md),
                )])
            )
        return messages

    def _rows_to_model_messages(self, rows: list[dict]) -> list[ModelMessage]:
        """Convert DB message rows to pydantic-ai ModelMessage list.

        Handles message_type values:
        - user → ModelRequest with UserPromptPart
        - system → ModelRequest with SystemPromptPart
        - assistant → ModelResponse with TextPart
        - tool_call → skipped (persisted for observability, not replayed to LLM)
        - tool_response → skipped (persisted for observability, not replayed to LLM)
        - observation → ModelRequest with UserPromptPart (observations are context)
        - memory → ModelRequest with SystemPromptPart (injected memories)
        - think → skipped (internal reasoning)
        """
        messages: list[ModelMessage] = []
        for row in rows:
            mt = row.get("message_type", "user")
            content = row.get("content") or ""

            if mt == "user":
                messages.append(
                    ModelRequest(parts=[UserPromptPart(content=content)])
                )
            elif mt == "system":
                messages.append(
                    ModelRequest(parts=[SystemPromptPart(content=content)])
                )
            elif mt == "assistant":
                messages.append(
                    ModelResponse(parts=[TextPart(content=content)])
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
            # tool_call, tool_response, think, tool_result → skipped

        return messages

    # ------------------------------------------------------------------
    # Tool call extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_tool_calls(
        all_messages: list[ModelMessage] | None,
    ) -> list[dict]:
        """Extract tool calls paired with their responses from pydantic-ai messages.

        Iterates through the message sequence, finds ToolCallPart entries in
        ModelResponse objects, then looks for the matching ToolReturnPart in
        subsequent ModelRequest objects (matched by tool_call_id).

        Returns a list of dicts with call metadata AND the tool response content.
        The response is persisted in the content column for observability —
        especially important for ask_agent structured output delegation.
        """
        if not all_messages:
            return []

        # First pass: index all ToolReturnParts by tool_call_id
        returns: dict[str, str] = {}
        for msg in all_messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart) and part.tool_call_id:
                        returns[part.tool_call_id] = part.content if isinstance(part.content, str) else str(part.content)

        # Second pass: extract ToolCallParts and pair with returns
        calls: list[dict] = []
        for msg in all_messages:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:  # type: ignore[assignment]
                    if isinstance(part, ToolCallPart):
                        args = part.args
                        if isinstance(args, str):
                            import json
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, TypeError):
                                args = {"raw": args}
                        call_id = part.tool_call_id or ""
                        calls.append({
                            "tool_name": part.tool_name,
                            "tool_call_id": call_id,
                            "arguments": args if isinstance(args, dict) else {},
                            "result": returns.get(call_id),
                        })
        return calls

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
        all_messages: list[ModelMessage] | None = None,
        background_compaction: bool = True,
        moment_threshold: int = 6000,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int | None = None,
        model: str | None = None,
        agent_name: str | None = None,
    ) -> None:
        """Persist a conversation turn — user, tool_call(s), assistant.

        When all_messages contains tool calls, inserts messages individually
        in order: user → tool_call(s) → assistant. Tool call rows store both
        the call metadata (name, args, id) in tool_calls JSONB and the tool
        response in content. This is especially important for ask_agent
        delegation where structured output is the artifact.

        When no tool calls are present, uses rem_persist_turn for efficiency
        (single SQL round-trip).

        When tenant_id is provided, message content is encrypted with the
        tenant's DEK and encryption_level is stamped on the message rows.
        Sealed mode is capped to 'platform' for chat messages — the server
        must be able to decrypt history for the LLM.
        """
        from p8.ontology.types import Message
        from uuid import uuid4

        # Resolve encryption mode
        encryption_level: str | None = None
        store_user = user_prompt
        store_assistant = assistant_text
        user_msg_id: UUID | None = None
        asst_msg_id: UUID | None = None
        if tenant_id:
            await self.encryption.get_dek(tenant_id)
            mode = await self.encryption.get_tenant_mode(tenant_id)
            if mode == "sealed":
                mode = "platform"
            encryption_level = mode if mode != "disabled" else "disabled"
            if mode in ("platform", "client"):
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

        # Extract tool calls from pydantic-ai messages
        tool_calls = self._extract_tool_calls(all_messages)

        if tool_calls:
            # Slow path: insert user, tool_call/tool_response pairs, assistant.
            # Pass plain text — Repository.upsert() handles encryption via
            # tenant_id. Using store_user/store_assistant here would double-encrypt.
            await self.memory.persist_message(
                session_id, "user", user_prompt,
                user_id=user_id, tenant_id=tenant_id,
            )
            for tc in tool_calls:
                # tool_call row — call metadata, no content
                await self.memory.persist_message(
                    session_id, "tool_call", None,
                    user_id=user_id, tenant_id=tenant_id,
                    token_count=0,
                    agent_name=agent_name,
                    tool_calls={
                        "name": tc["tool_name"],
                        "id": tc["tool_call_id"],
                        "arguments": tc["arguments"],
                    },
                )
                # tool_response row — the result content
                if tc.get("result") is not None:
                    await self.memory.persist_message(
                        session_id, "tool_response", tc["result"],
                        user_id=user_id, tenant_id=tenant_id,
                        agent_name=agent_name,
                        tool_calls={
                            "name": tc["tool_name"],
                            "id": tc["tool_call_id"],
                        },
                    )
            # Assistant message with usage metrics
            await self.memory.persist_message(
                session_id, "assistant", assistant_text,
                user_id=user_id, tenant_id=tenant_id,
                agent_name=agent_name, model=model,
                input_tokens=input_tokens, output_tokens=output_tokens,
                latency_ms=latency_ms, encryption_level=encryption_level,
            )
        else:
            # Fast path: single SQL round-trip via rem_persist_turn
            await self.db.rem_persist_turn(
                session_id, store_user, store_assistant,
                user_id=user_id, tenant_id=tenant_id,
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

    async def execute_chained_tool(
        self,
        structured_output: dict,
        *,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        tenant_id: str | None = None,
    ) -> dict | None:
        """Auto-invoke a chained tool with the agent's structured output.

        Called after an agent run when the schema has both ``structured_output``
        and ``chained_tool`` set. Looks up the tool by name from the tool
        registry, calls it with the structured output, and persists both a
        tool_call and tool_response message pair to the session.

        Returns the tool result dict, or None if chaining is not applicable.
        Never propagates exceptions — the agent's structured output is still valid.
        """
        tool_name = self.agent_schema.chained_tool
        if not tool_name or not self.agent_schema.structured_output:
            return None

        from p8.api.tools import get_tool_fn

        tool_fn = get_tool_fn(tool_name)
        if tool_fn is None:
            log.warning("Chained tool '%s' not found in registry", tool_name)
            return None

        import copy
        import json
        from uuid import uuid4

        call_id = str(uuid4())
        tool_result: dict | str

        # Deep-copy: tools may mutate their input (e.g. save_moments pops
        # affinity_fragments). The original dict is persisted as arguments.
        tool_input = copy.deepcopy(structured_output)

        try:
            tool_result = await tool_fn(**tool_input)
        except TypeError:
            # Tool doesn't accept **kwargs — try passing as positional
            try:
                tool_result = await tool_fn(tool_input)
            except Exception as e:
                log.error("Chained tool '%s' failed: %s", tool_name, e)
                tool_result = {"status": "error", "error": str(e)}
        except Exception as e:
            log.error("Chained tool '%s' failed: %s", tool_name, e)
            tool_result = {"status": "error", "error": str(e)}

        # Persist tool_call + tool_response to session
        if session_id is not None:
            result_str = json.dumps(tool_result, default=str) if isinstance(tool_result, dict) else str(tool_result)
            await self.memory.persist_message(
                session_id, "tool_call", None,
                user_id=user_id, tenant_id=tenant_id,
                token_count=0,
                agent_name=self.agent_schema.name,
                tool_calls={
                    "name": tool_name,
                    "id": call_id,
                    "arguments": structured_output,
                },
            )
            await self.memory.persist_message(
                session_id, "tool_response", result_str,
                user_id=user_id, tenant_id=tenant_id,
                agent_name=self.agent_schema.name,
                tool_calls={
                    "name": tool_name,
                    "id": call_id,
                },
            )

        return tool_result if isinstance(tool_result, dict) else {"result": tool_result}

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


# ---------------------------------------------------------------------------
# Resource → Tool wrappers
# ---------------------------------------------------------------------------


def _create_static_resource_tool(
    tool_name: str,
    uri: str,
    description: str,
) -> Any:
    """Create a no-arg tool that reads a static MCP resource URI."""

    async def resource_tool() -> str:
        from p8.api.mcp_server import get_mcp_server

        mcp = get_mcp_server()
        result = await mcp.read_resource(uri)
        # ResourceResult.contents is str | bytes | list[ResourceContent]
        contents = result.contents
        if isinstance(contents, (str, bytes)):
            return contents if isinstance(contents, str) else contents.decode()
        # list[ResourceContent] — join text items
        return "\n".join(
            str(c.content) if hasattr(c, "content") else str(c) for c in contents
        )

    resource_tool.__name__ = tool_name
    resource_tool.__qualname__ = tool_name
    resource_tool.__doc__ = description
    return resource_tool


def _create_parameterized_resource_tool(
    tool_name: str,
    uri_template: str,
    params: list[str],
    description: str,
) -> Any:
    """Create a tool with parameters matching URI template variables.

    Context-aware parameters (``user_id``, ``session_id``) are auto-resolved
    from the per-request tool context and hidden from the LLM signature.
    Remaining parameters become LLM-visible keyword arguments.

    Builds a proper ``__signature__`` so pydantic-ai discovers the
    parameters for LLM tool-call generation.
    """
    import inspect
    from p8.api.tools import get_session_id, get_user_id

    # Parameters auto-resolved from tool context — not exposed to the LLM
    _CONTEXT_RESOLVERS: dict[str, Any] = {
        "user_id": get_user_id,
        "session_id": get_session_id,
    }
    context_params = [p for p in params if p in _CONTEXT_RESOLVERS]
    llm_params = [p for p in params if p not in _CONTEXT_RESOLVERS]

    async def _impl(**kwargs: str) -> str:
        from p8.api.mcp_server import get_mcp_server

        # Inject context-resolved values
        for cp in context_params:
            val = _CONTEXT_RESOLVERS[cp]()
            if val is not None:
                kwargs[cp] = str(val)
            elif cp not in kwargs:
                return f"Error: {cp} not available in context"

        mcp = get_mcp_server()
        uri = uri_template.format(**kwargs)
        result = await mcp.read_resource(uri)
        contents = result.contents
        if isinstance(contents, (str, bytes)):
            return contents if isinstance(contents, str) else contents.decode()
        return "\n".join(
            str(c.content) if hasattr(c, "content") else str(c) for c in contents
        )

    if llm_params:
        # Some params still need to come from the LLM
        sig_params = [
            inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, annotation=str)
            for p in llm_params
        ]
        sig = inspect.Signature(sig_params, return_annotation=str)

        async def resource_tool(**kwargs: str) -> str:
            return await _impl(**kwargs)

        resource_tool.__signature__ = sig  # type: ignore[attr-defined]
        resource_tool.__annotations__ = {p: str for p in llm_params}
        resource_tool.__annotations__["return"] = str
    else:
        # All params are context-resolved — zero-arg tool for the LLM
        async def resource_tool() -> str:  # type: ignore[misc]
            return await _impl()

    resource_tool.__name__ = tool_name
    resource_tool.__qualname__ = tool_name
    resource_tool.__doc__ = description
    return resource_tool
