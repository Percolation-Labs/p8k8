"""ask_agent tool — invoke another agent by name for multi-agent orchestration.

When a parent agent calls ask_agent, the child agent streams its response
in real-time via a ContextVar event sink (``asyncio.Queue``). The parent's
streaming loop picks up these events via ``asyncio.wait(FIRST_COMPLETED)``
and forwards them to the client's SSE stream as they arrive.

Event flow::

    Parent streaming loop
        ├── child_event_sink = asyncio.Queue()
        ├── set_child_event_sink(child_event_sink)  # ContextVar
        └── agent.iter(prompt) for parent
              └── CallToolsNode → pydantic-ai calls ask_agent()
                    ├── get_child_event_sink()  # reads the same Queue
                    └── agent.iter(prompt) for child
                          ├── ModelRequestNode.stream() → PartDeltaEvent
                          │     → push {"type": "child_content", ...} to queue
                          └── CallToolsNode.stream() → FunctionToolCallEvent
                                → push {"type": "child_tool_start/result", ...}

    Meanwhile the parent's multiplexer:
        asyncio.wait({pending_tool, pending_child}, FIRST_COMPLETED)
        → yields child events to SSE as soon as they arrive

When no event sink is available (CLI mode), falls back to ``agent.run()``
for a non-streaming call.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
)

from p8.agentic.adapter import AgentAdapter
from p8.agentic.delegate import get_child_event_sink
from p8.api.tools import get_db, get_encryption


async def _run_with_streaming(
    agent: Agent,
    prompt: str,
    agent_name: str,
    event_sink: Any,
) -> dict[str, Any]:
    """Run child agent with agent.iter(), pushing events to the sink in real-time."""
    accumulated_content: list[str] = []

    async with agent.iter(prompt) as agent_run:
        async for node in agent_run:
            if Agent.is_model_request_node(node):
                async with node.stream(agent_run.ctx) as request_stream:
                    async for event in request_stream:
                        if isinstance(event, PartStartEvent):
                            if isinstance(event.part, TextPart) and event.part.content:
                                content = event.part.content
                                accumulated_content.append(content)
                                await event_sink.put({
                                    "type": "child_content",
                                    "agent_name": agent_name,
                                    "content": content,
                                })
                        elif isinstance(event, PartDeltaEvent):
                            if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
                                content = event.delta.content_delta
                                accumulated_content.append(content)
                                await event_sink.put({
                                    "type": "child_content",
                                    "agent_name": agent_name,
                                    "content": content,
                                })

            elif Agent.is_call_tools_node(node):
                async with node.stream(agent_run.ctx) as tools_stream:
                    async for tool_event in tools_stream:
                        if isinstance(tool_event, FunctionToolCallEvent):
                            tool_args = tool_event.part.args_as_json_str()
                            await event_sink.put({
                                "type": "child_tool_start",
                                "agent_name": agent_name,
                                "tool_name": tool_event.part.tool_name,
                                "tool_call_id": tool_event.tool_call_id,
                                "arguments": tool_args,
                            })
                        elif isinstance(tool_event, FunctionToolResultEvent):
                            result_content = (
                                tool_event.result.model_response_str()
                                if isinstance(tool_event.result, ToolReturnPart)
                                else str(tool_event.result.content)
                            )
                            await event_sink.put({
                                "type": "child_tool_result",
                                "agent_name": agent_name,
                                "tool_name": tool_event.result.tool_name,
                                "tool_call_id": tool_event.tool_call_id,
                                "result": result_content,
                            })

    assert agent_run.result is not None, "Agent run completed without a result"
    output = agent_run.result.output
    text_response = "".join(accumulated_content) or str(output)
    is_structured = not isinstance(output, str)

    if is_structured and hasattr(output, "model_dump"):
        output_data = output.model_dump()
    elif isinstance(output, dict):
        output_data = output
    else:
        output_data = text_response

    return {
        "status": "success",
        "output": output_data,
        "text_response": text_response,
        "agent_schema": agent_name,
        "is_structured_output": is_structured,
    }


async def ask_agent(
    agent_name: str,
    input_text: str,
    input_data: dict[str, Any] | None = None,
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Invoke another agent by name and return its response.

    Enables multi-agent orchestration by allowing one agent to delegate
    to another. The child agent runs with its own schema, tools, and
    system prompt.

    When called from the streaming chat endpoint, a child event sink
    (``asyncio.Queue``) is available via ContextVar. The child agent
    streams its content tokens, tool calls, and tool results to this
    queue in real-time using ``agent.iter()``. The parent's multiplexer
    picks these up and forwards them to the client immediately.

    When no event sink is available (e.g. CLI mode), falls back to
    ``agent.run()`` for a non-streaming call.

    Args:
        agent_name: Agent schema name (must exist in schemas table with kind='agent')
        input_text: The prompt to send to the agent
        input_data: Optional structured input data (appended to prompt as JSON)
        user_id: Optional user ID for context

    Returns:
        Dict with status, output, text_response, agent_schema, and
        is_structured_output keys.
    """
    db = get_db()
    encryption = get_encryption()

    try:
        adapter = await AgentAdapter.from_schema_name(agent_name, db, encryption)
    except ValueError:
        return {
            "status": "error",
            "output": "",
            "text_response": "",
            "agent_schema": agent_name,
            "error": f"Agent '{agent_name}' not found",
        }

    prompt = input_text
    if input_data:
        prompt = f"{input_text}\n\nInput data: {json.dumps(input_data)}"

    try:
        agent = adapter.build_agent()
        event_sink = get_child_event_sink()

        if event_sink is not None:
            # Streaming mode: use agent.iter() and push events to sink
            return await _run_with_streaming(agent, prompt, agent_name, event_sink)
        else:
            # Non-streaming mode (CLI): use agent.run()
            result = await agent.run(prompt)
            output = result.output
            text_response = str(output)
            is_structured = not isinstance(output, str)
            if is_structured and hasattr(output, "model_dump"):
                output_data = output.model_dump()
            elif isinstance(output, dict):
                output_data = output
            else:
                output_data = text_response

            return {
                "status": "success",
                "output": output_data,
                "text_response": text_response,
                "agent_schema": agent_name,
                "is_structured_output": is_structured,
            }
    except Exception as e:
        return {
            "status": "error",
            "output": "",
            "text_response": "",
            "agent_schema": agent_name,
            "error": str(e),
        }
