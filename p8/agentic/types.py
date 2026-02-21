"""Typed models for the agentic runtime.

Agent config, context attributes, routing state, streaming events,
and tool references — all as Pydantic models.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, create_model
from pydantic_ai import UsageLimits


# ---------------------------------------------------------------------------
# Agent config (extracted from schema json_schema)
# ---------------------------------------------------------------------------


class ToolReference(BaseModel):
    """Pointer to a remote tool on a server."""

    name: str
    server: str = "local"
    protocol: str = "mcp"  # mcp | openapi
    description: str | None = None


class ResourceReference(BaseModel):
    """Pointer to an MCP resource for context injection."""

    uri: str
    name: str | None = None
    description: str | None = None


class AgentUsageLimits(BaseModel):
    """Token and request limits for agent execution.

    Maps to ``pydantic_ai.UsageLimits`` at runtime. Declaring limits
    in schema config prevents runaway agents from consuming unbounded tokens.
    """

    request_limit: int | None = None
    tool_calls_limit: int | None = None
    input_tokens_limit: int | None = None
    output_tokens_limit: int | None = None
    total_tokens_limit: int | None = None

    def to_pydantic_ai(self) -> UsageLimits:
        """Convert to ``pydantic_ai.UsageLimits``."""
        kwargs: dict[str, Any] = {}
        if self.request_limit is not None:
            kwargs["request_limit"] = self.request_limit
        if self.tool_calls_limit is not None:
            kwargs["tool_calls_limit"] = self.tool_calls_limit
        if self.input_tokens_limit is not None:
            kwargs["input_tokens_limit"] = self.input_tokens_limit
        if self.output_tokens_limit is not None:
            kwargs["output_tokens_limit"] = self.output_tokens_limit
        if self.total_tokens_limit is not None:
            kwargs["total_tokens_limit"] = self.total_tokens_limit
        return UsageLimits(**kwargs)


class AgentConfig(BaseModel):
    """Runtime config extracted from schema json_schema.

    This is what lives in the ``json_schema`` JSONB column of a schema row
    with ``kind='agent'``. The adapter reads this to build a pydantic-ai Agent.

    Structured output:
        When ``structured_output=True`` and ``response_schema`` contains
        ``properties``, a dynamic Pydantic model is generated via
        ``to_output_model()``. When ``structured_output=False`` but properties
        exist, ``to_prompt_guidance()`` produces human-readable field guidance
        that can be appended to the system prompt.
    """

    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    max_iterations: int = 10
    structured_output: bool = False
    response_schema: dict | None = None
    tools: list[ToolReference] = Field(default_factory=list)
    resources: list[ResourceReference] = Field(default_factory=list)
    limits: AgentUsageLimits | None = None

    # Routing
    routing_enabled: bool = True  # opt in to default routing table
    routing_model: str | None = None  # override routing classifier model
    routing_max_turns: int = 20

    # Observation
    observation_mode: str = "sync"  # sync | parallel | disabled
    observation_prompt: str | None = None

    @classmethod
    def from_json_schema(cls, raw: dict | None) -> AgentConfig:
        """Parse json_schema JSONB into typed config, tolerating extra keys."""
        if not raw:
            return cls()
        # Only take known fields
        known = cls.model_fields
        filtered = {k: v for k, v in raw.items() if k in known}
        # Normalize tool/resource entries
        if "tools" in filtered:
            filtered["tools"] = [
                t if isinstance(t, dict) else {"name": t}
                for t in filtered["tools"]
            ]
        if "resources" in filtered:
            filtered["resources"] = [
                r if isinstance(r, dict) else {"uri": r}
                for r in filtered["resources"]
            ]
        # Normalize limits
        if "limits" in filtered and isinstance(filtered["limits"], dict):
            filtered["limits"] = AgentUsageLimits(**filtered["limits"])
        return cls.model_validate(filtered)

    # ------------------------------------------------------------------
    # Output model generation
    # ------------------------------------------------------------------

    @property
    def _properties(self) -> dict[str, Any]:
        """Extract properties from response_schema."""
        if not self.response_schema:
            return {}
        return self.response_schema.get("properties", {})

    @property
    def _required(self) -> list[str]:
        """Extract required fields from response_schema."""
        if not self.response_schema:
            return []
        return self.response_schema.get("required", [])

    def to_output_model(self) -> type[BaseModel] | type[str]:
        """Generate a dynamic Pydantic model from response_schema properties.

        If ``structured_output`` is disabled or no properties are defined,
        returns ``str`` (the agent produces plain text).

        Otherwise, builds a Pydantic model at runtime using
        ``pydantic.create_model``.

        Example response_schema::

            {
                "properties": {
                    "answer": {"type": "string"},
                    "confidence": {"type": "number"}
                },
                "required": ["answer"]
            }
        """
        if not self.structured_output or not self._properties:
            return str

        type_map: dict[str, type] = {
            "string": str,
            "number": float,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        field_definitions: dict[str, Any] = {}
        required_set = set(self._required)

        for field_name, field_def in self._properties.items():
            field_type_str = (
                field_def.get("type", "string")
                if isinstance(field_def, dict)
                else "string"
            )
            python_type = type_map.get(field_type_str, str)

            if field_name in required_set:
                field_definitions[field_name] = (python_type, ...)
            else:
                field_definitions[field_name] = (python_type | None, None)

        # Generate a class name from the agent config context
        return create_model("AgentOutput", **field_definitions)

    def to_prompt_guidance(self) -> str:
        """Convert response_schema properties to human-readable prompt guidance.

        When structured output is disabled, the agent still benefits from
        knowing what fields it *should* produce. This generates a
        natural-language description of the expected output shape that can
        be appended to the system prompt.

        Returns empty string if no properties are defined.
        """
        if not self._properties:
            return ""

        lines = ["Your response should include the following fields:"]
        required_set = set(self._required)
        for field_name, field_def in self._properties.items():
            field_type = (
                field_def.get("type", "string")
                if isinstance(field_def, dict)
                else "string"
            )
            desc = (
                field_def.get("description", "")
                if isinstance(field_def, dict)
                else ""
            )
            required_marker = " (required)" if field_name in required_set else " (optional)"
            line = f"- **{field_name}** ({field_type}{required_marker})"
            if desc:
                line += f": {desc}"
            lines.append(line)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context attributes — injected into every agent's message stream
# ---------------------------------------------------------------------------


class ContextAttributes(BaseModel):
    """Runtime context loaded per-request and injected via ContextInjector.

    These attributes are the per-request facts every agent should see:
    current date/time, user identity, session, and the routing table.
    """

    current_date: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    current_time: str = Field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    user_id: UUID | None = None
    user_email: str | None = None
    user_name: str | None = None
    session_id: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    session_metadata: dict | None = None
    routing_table: dict = Field(default_factory=dict)

    def render(self) -> str:
        """Render context attributes as a text block for injection."""
        lines = [
            "[Context]",
            f"Date: {self.current_date}",
            f"Time: {self.current_time}",
        ]
        if self.user_id:
            lines.append(f"User ID: {self.user_id}")
        if self.user_email:
            lines.append(f"User email: {self.user_email}")
        if self.user_name:
            lines.append(f"User: {self.user_name}")
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        if self.agent_name:
            lines.append(f"Agent: {self.agent_name}")
        if self.session_name or self.session_metadata:
            lines.append("")
            lines.append("## Session Context")
            if self.session_name:
                lines.append(f"Session: {self.session_name}")
            if self.session_metadata:
                lines.append(f"Context: {json.dumps(self.session_metadata)}")
            lines.append("Use REM LOOKUP to retrieve full details for any keys listed above.")
        if self.routing_table:
            lines.append(f"Routing: {json.dumps(self.routing_table)}")
        return "\n".join(lines)

    # Keep backward compat alias
    def to_system_message(self) -> str:
        """Alias for render() — backward compatibility."""
        return self.render()


class ContextInjector:
    """Injects runtime context attributes into agent runs.

    Uses pydantic-ai's ``instructions`` parameter — the native mechanism
    for adding system-level content after the agent's system prompt.
    This keeps context attributes separate from message history and
    ensures they appear in the correct position (after system prompt,
    before conversation history).

    The injector is extensible: subclass and override ``build_instructions()``
    to add custom sections, or pass ``extra_sections`` to ``__init__`` for
    ad-hoc additions.

    Usage::

        injector = ContextInjector(context_attrs)

        # Pass to agent.run / agent.iter / adapter.run_stream
        result = await agent.run(prompt, instructions=injector.instructions)

        # Or with AGUIAdapter
        adapter.run_stream(instructions=injector.instructions, ...)

    Future positions:
        The ``position`` field is reserved for future use. Currently only
        ``"after_system_prompt"`` is supported (via ``instructions``).
        Other positions (e.g. ``"before_last_user"``, ``"tool_context"``)
        would require different injection mechanisms.
    """

    def __init__(
        self,
        context_attrs: ContextAttributes,
        *,
        extra_sections: list[str] | None = None,
        position: str = "after_system_prompt",
    ):
        self.context_attrs = context_attrs
        self.extra_sections = extra_sections or []
        self.position = position

    def build_instructions(self) -> str:
        """Build the full instructions string from context + extras.

        Override in subclasses to customize what gets injected.
        """
        parts = [self.context_attrs.render()]
        parts.extend(self.extra_sections)
        return "\n\n".join(parts)

    @property
    def instructions(self) -> str:
        """The instructions string to pass to pydantic-ai.

        Compatible with ``agent.run(instructions=...)``,
        ``agent.iter(instructions=...)``, and
        ``AGUIAdapter.run_stream(instructions=...)``.
        """
        return self.build_instructions()


# ---------------------------------------------------------------------------
# Routing state — lives in session metadata
# ---------------------------------------------------------------------------


class RoutingState(BaseModel):
    """Routing table stored in session metadata.routing.

    Implements lazy routing (default): active agent persists until
    it signals completion or hits max_turns.
    """

    active_agent: str | None = None
    state: str = "idle"  # idle | executing | complete | re-evaluate | escalated
    target: str = "complete"
    turn_count: int = 0
    max_turns: int = 20
    fallback: str = "general"
    escalation: str | None = None
    delegation: dict | None = None  # nested child delegation state
    transitions: dict = Field(default_factory=lambda: {
        "executing": {
            "on_complete": "idle",
            "on_escalate": "escalated",
            "on_max_turns": "re-evaluate",
        },
        "idle": {"on_message": "executing"},
        "escalated": {"on_resolve": "idle"},
    })

    def should_reclassify(self) -> bool:
        """Whether the router needs to classify the next message."""
        if self.state == "idle":
            return True
        if self.state == "re-evaluate":
            return True
        if self.state == "executing" and self.turn_count >= self.max_turns:
            return True
        return False

    def activate(self, agent_name: str, *, max_turns: int | None = None) -> None:
        """Transition to executing state with a given agent."""
        self.active_agent = agent_name
        self.state = "executing"
        self.turn_count = 0
        if max_turns is not None:
            self.max_turns = max_turns

    def increment_turn(self) -> None:
        """Increment turn count. Transitions to re-evaluate if over limit."""
        self.turn_count += 1
        if self.turn_count >= self.max_turns:
            self.state = "re-evaluate"

    def complete(self) -> None:
        """Agent signals completion."""
        self.state = "idle"
        self.active_agent = self.fallback


# ---------------------------------------------------------------------------
# Streaming event models
# ---------------------------------------------------------------------------


class ToolCallEvent(BaseModel):
    """SSE event for tool call start/completion."""

    type: str = "tool_call"
    tool_name: str
    tool_id: str
    status: str = "started"  # started | completed
    arguments: dict[str, Any] | None = None
    result: Any = None


class ActionEvent(BaseModel):
    """SSE event for agent actions (observation, elicit, delegate)."""

    type: str = "action"
    action_type: str  # observation | elicit | delegate | escalate
    payload: dict[str, Any] | None = None


class MetadataEvent(BaseModel):
    """SSE event for response metadata."""

    type: str = "metadata"
    message_id: str | None = None
    session_id: str | None = None
    agent_schema: str | None = None
    responding_agent: str | None = None
    confidence: float | None = None
    sources: list[str] | None = None
    extra: dict[str, Any] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    model: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


class ProgressEvent(BaseModel):
    """SSE event for multi-step progress."""

    type: str = "progress"
    step: int = 1
    total_steps: int = 3
    label: str = "Processing"
    status: str = "in_progress"


class DoneEvent(BaseModel):
    """SSE event signalling stream end."""

    type: str = "done"
    reason: str = "stop"


class ErrorEvent(BaseModel):
    """SSE event for streaming errors."""

    type: str = "error"
    code: str = "stream_error"
    message: str
    details: dict[str, Any] | None = None
    recoverable: bool = True


class ChildContentEvent(BaseModel):
    """SSE event for content from a delegated child agent."""

    type: str = "child_content"
    agent_name: str
    content: str


class ChildToolEvent(BaseModel):
    """SSE event for tool calls made by a child agent."""

    type: str = "child_tool_start"
    agent_name: str
    tool_name: str
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any = None


# ---------------------------------------------------------------------------
# Streaming state tracker
# ---------------------------------------------------------------------------


@dataclass
class StreamingState:
    """Mutable state for an in-progress streaming response."""

    request_id: str = field(
        default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}"
    )
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: int = field(default_factory=lambda: int(time.time()))
    start_time: float = field(default_factory=time.monotonic)
    model: str = "unknown"

    current_text: str = ""
    is_first_chunk: bool = True

    active_tool_calls: dict[int, tuple[str, str]] = field(default_factory=dict)
    pending_tool_data: dict[str, dict[str, Any]] = field(default_factory=dict)

    metadata: dict[str, Any] = field(default_factory=dict)
    responding_agent: str | None = None

    def latency_ms(self) -> int:
        return int((time.monotonic() - self.start_time) * 1000)

    def mark_first_chunk_sent(self) -> None:
        self.is_first_chunk = False

    def append_content(self, content: str) -> None:
        self.current_text += content

    def register_tool_call(
        self, tool_name: str, tool_id: str, index: int,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.active_tool_calls[index] = (tool_name, tool_id)
        self.pending_tool_data[tool_id] = {
            "name": tool_name,
            "arguments": arguments or {},
        }

    def complete_tool_call(self, tool_id: str, result: Any) -> dict[str, Any] | None:
        data = self.pending_tool_data.pop(tool_id, None)
        if data is None:
            return None
        for idx, (_, tid) in list(self.active_tool_calls.items()):
            if tid == tool_id:
                del self.active_tool_calls[idx]
                break
        data["result"] = result
        return data
