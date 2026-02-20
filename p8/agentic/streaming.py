"""SSE formatters for streaming agent responses.

Provides format_sse_event(), format_content_chunk(), format_done(),
and format_child_event() for the OpenAI-compatible and custom event
wire formats.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from p8.agentic.types import (
    ChildContentEvent,
    ChildToolEvent,
    StreamingState,
)


def format_sse_event(event: BaseModel) -> str:
    """Serialize a Pydantic event model as a named SSE event.

    Format::

        event: <type>
        data: <json>

    """
    event_type = getattr(event, "type", "message")
    data = event.model_dump(exclude_none=True)
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def format_content_chunk(
    content: str,
    state: StreamingState,
    *,
    finish_reason: str | None = None,
) -> str:
    """Format a text delta as an OpenAI-compatible SSE chunk."""
    delta: dict[str, Any] = {}
    if state.is_first_chunk:
        delta["role"] = "assistant"
    if content:
        delta["content"] = content

    chunk_data = {
        "id": state.request_id,
        "object": "chat.completion.chunk",
        "created": state.created_at,
        "model": state.model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }

    if content:
        state.append_content(content)
    if state.is_first_chunk:
        state.mark_first_chunk_sent()

    return f"data: {json.dumps(chunk_data)}\n\n"


def format_done() -> str:
    """Format the final [DONE] SSE marker."""
    return "data: [DONE]\n\n"


def format_child_event(agent_name: str, raw_sse_event: str) -> str:
    """Transform a raw SSE event from a child agent into a parent-namespaced event.

    Converts tool_call → child_tool_start/child_tool_result,
    content deltas → child_content, skips [DONE].
    """
    raw = raw_sse_event.strip()
    if not raw or raw == "data: [DONE]":
        return ""

    event_type = ""
    data_str = ""
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data_str = line[6:]

    if not data_str:
        return ""

    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return ""

    if data.get("type") == "done" or event_type == "done":
        return ""

    # Tool call events → child_tool_*
    if event_type == "tool_call" or data.get("type") == "tool_call":
        status = data.get("status", "started")
        child_event = ChildToolEvent(
            type="child_tool_result" if status == "completed" else "child_tool_start",
            agent_name=agent_name,
            tool_name=data.get("tool_name", ""),
            tool_call_id=data.get("tool_id"),
            arguments=data.get("arguments") if status != "completed" else None,
            result=data.get("result") if status == "completed" else None,
        )
        return format_sse_event(child_event)

    # Content deltas → child_content
    choices = data.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if content:
            return format_sse_event(ChildContentEvent(
                agent_name=agent_name,
                content=content,
            ))

    return ""
