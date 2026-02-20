"""Multi-agent delegation — child event forwarding infrastructure.

Provides a ContextVar-based event sink that connects child agents (producers)
to the parent's streaming loop (consumer).

Producer (``ask_agent``):
    Reads the event sink via ``get_child_event_sink()``. If a sink exists,
    runs the child agent with ``agent.iter()``, iterating nodes and pushing
    ``child_content``, ``child_tool_start``, and ``child_tool_result`` dicts
    to the queue in real-time as tokens arrive.

Consumer (``chat.py`` router):
    Creates an ``asyncio.Queue``, stores it via ``set_child_event_sink()``,
    then runs the parent agent with ``AGUIAdapter.run_stream()``. A
    multiplexer wraps the AG-UI event stream and races each parent event
    against the child queue using ``asyncio.wait(FIRST_COMPLETED)``.
    Child events are emitted as AG-UI ``CustomEvent`` objects.

This decouples the child's streaming output from the parent's tool
execution, enabling real-time token-by-token delivery of delegated
agent content to the client.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from p8.agentic.streaming import format_child_event


# ---------------------------------------------------------------------------
# Child event sink (context variable)
# ---------------------------------------------------------------------------

_child_event_sink: ContextVar[asyncio.Queue[dict] | None] = ContextVar(
    "child_event_sink", default=None
)


def get_child_event_sink() -> asyncio.Queue[dict] | None:
    """Get the current child event sink queue, if set by a parent."""
    return _child_event_sink.get()


def set_child_event_sink(queue: asyncio.Queue[dict] | None) -> asyncio.Queue[dict] | None:
    """Set (or clear) the child event sink. Returns previous value for restoration."""
    previous = _child_event_sink.get()
    _child_event_sink.set(queue)
    return previous


# ---------------------------------------------------------------------------
# Child event forwarding (legacy SSE format — retained for custom streaming)
# ---------------------------------------------------------------------------


async def _forward_child_events(
    event_sink: asyncio.Queue[str],
    agent_name: str,
    sse_stream: Any,
) -> None:
    """Consume a child agent's SSE stream and forward events to the parent.

    This is the legacy SSE forwarding path. The primary mechanism is now
    the ``agent.iter()`` + dict-based event pushing in ``ask_agent``.
    """
    async for raw_event in sse_stream:
        transformed = format_child_event(agent_name, raw_event)
        if transformed:
            await event_sink.put(transformed)
