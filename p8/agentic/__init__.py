"""Agentic runtime — adapter, streaming, routing, delegation."""

from p8.agentic.adapter import BUILTIN_AGENTS, DELEGATE_TOOL_NAMES, SAMPLE_AGENT, AgentAdapter, register_sample_agent
from p8.agentic.delegate import get_child_event_sink, set_child_event_sink
from p8.agentic.routing import DefaultClassifier, Router, RouterClassifier, default_router
from p8.agentic.streaming import (
    format_child_event,
    format_content_chunk,
    format_done,
    format_sse_event,
)
from p8.agentic.types import (
    ActionEvent,
    AgentConfig,
    AgentUsageLimits,
    ChildContentEvent,
    ChildToolEvent,
    ContextAttributes,
    DoneEvent,
    ErrorEvent,
    MetadataEvent,
    ProgressEvent,
    ResourceReference,
    RoutingState,
    StreamingState,
    ToolCallEvent,
    ToolReference,
)

__all__ = [
    # Adapter
    "AgentAdapter",
    "SAMPLE_AGENT",
    "BUILTIN_AGENTS",
    "DELEGATE_TOOL_NAMES",
    "register_sample_agent",
    # Types
    "AgentConfig",
    "AgentUsageLimits",
    "ToolReference",
    "ResourceReference",
    "ContextAttributes",
    "RoutingState",
    "StreamingState",
    # Events
    "ToolCallEvent",
    "ActionEvent",
    "MetadataEvent",
    "ProgressEvent",
    "DoneEvent",
    "ErrorEvent",
    "ChildContentEvent",
    "ChildToolEvent",
    # Streaming
    "format_sse_event",
    "format_content_chunk",
    "format_done",
    "format_child_event",
    # Routing
    "Router",
    "RouterClassifier",
    "DefaultClassifier",
    "default_router",
    # Delegation
    "get_child_event_sink",
    "set_child_event_sink",
]
