"""Capture the actual payload pydantic-ai sends to the LLM.

Uses FunctionModel to intercept the call at the exact point where
pydantic-ai would normally POST to OpenAI. This is the same data
you'd see in openai._base_client DEBUG logs with `p8 chat --debug`.

Builds agents via the real AgentAdapter.build_agent() path with
a live MCP server so all tools are resolved.

Usage:
    uv run python tests/data/examples/capture_payloads.py

Outputs:
    tests/data/examples/intercept_unstructured.yaml
    tests/data/examples/intercept_structured.yaml
"""

import asyncio

import yaml
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from p8.agentic.adapter import AgentAdapter
from p8.agentic.core_agents import DREAMING_AGENT, GENERAL_AGENT
from p8.api.tools import init_tools
from p8.ontology.types import Schema
from p8.services.repository import Repository
import p8.services.bootstrap as _svc


def format_payload(messages: list[ModelMessage], info: AgentInfo, label: str) -> dict:
    """Format captured pydantic-ai data into the OpenAI request shape."""
    msg_list = []
    for msg in messages:
        for part in msg.parts:
            if isinstance(part, SystemPromptPart):
                msg_list.append({"role": "system", "content": part.content})
            elif isinstance(part, UserPromptPart):
                msg_list.append({"role": "user", "content": part.content})

    tools = []
    for t in info.function_tools or []:
        tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_json_schema,
            },
        })

    output_tools = []
    for t in info.output_tools or []:
        output_tools.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters_json_schema,
            },
        })

    ms = {}
    if info.model_settings:
        for k in ("temperature", "max_tokens", "top_p"):
            v = info.model_settings.get(k)
            if v is not None:
                ms[k] = v

    payload = {"label": label, **ms, "stream": True}
    if info.instructions:
        msg_list.append({"role": "system", "content": f"[instructions]\n{info.instructions}"})
    payload["messages"] = msg_list
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if output_tools:
        payload["output_tools"] = output_tools
    return payload


def save_yaml(payload: dict, filename: str) -> None:
    header = (
        f"# {'=' * 70}\n"
        f"# ACTUAL PAYLOAD pydantic-ai sends to the LLM\n"
        f"# {'=' * 70}\n"
        f"#\n"
        f"# Agent: {payload['label']}\n"
        f"#\n"
        f"# Captured via FunctionModel interception — the same data you see\n"
        f"# in openai._base_client DEBUG logs with: p8 chat --debug\n"
        f"#\n"
        f"# To regenerate: uv run python tests/data/examples/capture_payloads.py\n"
        f"# {'=' * 70}\n\n"
    )
    path = f"tests/data/examples/{filename}.yaml"
    yaml_content = yaml.dump(
        payload, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120,
    )
    with open(path, "w") as f:
        f.write(header)
        f.write(yaml_content)
    print(f"Saved {path}")
    print(f"  messages: {len(payload['messages'])}")
    print(f"  tools: {[t['function']['name'] for t in payload.get('tools', [])]}")
    print(f"  output_tools: {[t['function']['name'] for t in payload.get('output_tools', [])]}")
    print()


async def main():
    async with _svc.bootstrap_services() as (db, encryption, settings, file_service, *_rest):
        init_tools(db, encryption)
        repo = Repository(Schema, db, encryption)

        # --- General Agent (conversational / unstructured) ---
        general_captured: dict = {}

        def capture_general(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            general_captured["payload"] = format_payload(messages, info, "general (conversational)")
            return ModelResponse(parts=[TextPart(content="Captured.")])

        await repo.upsert(Schema(**GENERAL_AGENT.to_schema_dict()))
        adapter = await AgentAdapter.from_schema_name("general", db, encryption)
        agent = adapter.build_agent(model_override=FunctionModel(capture_general))
        injector = adapter.build_injector(user_id=None, session_id="demo-session")
        await agent.run("What did we talk about yesterday?", instructions=injector.instructions)

        # --- Dreaming Agent (structured output) ---
        dreaming_captured: dict = {}

        def capture_dreaming(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            dreaming_captured["payload"] = format_payload(messages, info, "dreaming-agent (structured)")
            return ModelResponse(parts=[TextPart(
                content='{"dream_moments":[],"search_questions":[],"cross_session_themes":[]}',
            )])

        await repo.upsert(Schema(**DREAMING_AGENT.to_schema_dict()))
        adapter2 = await AgentAdapter.from_schema_name("dreaming-agent", db, encryption)
        agent2 = adapter2.build_agent(model_override=FunctionModel(capture_dreaming))
        injector2 = adapter2.build_injector(user_id=None, session_id="dream-session")
        await agent2.run("Process recent sessions for cross-session themes.", instructions=injector2.instructions)

        save_yaml(general_captured["payload"], "intercept_unstructured")
        save_yaml(dreaming_captured["payload"], "intercept_structured")


if __name__ == "__main__":
    asyncio.run(main())
