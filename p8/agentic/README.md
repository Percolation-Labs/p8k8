# agentic/

Runtime for declarative agents: schema-driven agent construction, streaming, delegation, routing, and observability.

## Architecture Overview

```
agentic/
├── agent_schema.py  # AgentSchema — flat, unified schema for declarative agents
├── core_agents.py   # Built-in agents as Pydantic models (General, Dreaming, Sample)
├── types.py         # ContextInjector, RoutingState, streaming events, backward-compat aliases
├── adapter.py       # AgentAdapter — schema row → pydantic-ai Agent, persist_turn
├── delegate.py      # Child agent delegation + event sink (ContextVar Queue)
├── routing.py       # Lazy routing — classify, activate, persist active agent
├── streaming.py     # SSE event helpers
└── otel/
    ├── __init__.py  # Re-exports: setup_instrumentation, get_current_trace_context
    └── setup.py     # TracerProvider, OTLP exporter, SanitizingSpanExporter
```

## Core Concepts

### 1. Agent Schemas (Flat JSON Schema)

Agent schemas are **flat JSON Schema documents** — no nested `json_schema_extra` wrapper. The `description` is the system prompt. `properties` define thinking aides or structured output. Agent config (`tools`, `model`, `limits`) lives at the same level.

```yaml
type: object
name: general
description: |
  You are a friendly assistant with access to a knowledge base.
  Search before making claims about the user's data.
properties:
  user_intent:
    type: string
    description: "Classify: question, task, greeting, follow-up"
  topic:
    type: string
    description: Primary topic the user is asking about
tools:
  - name: search
    description: Query knowledge base using REM dialect
  - name: action
```

### 2. Properties as Thinking Aides

In **conversational mode** (`structured_output: false`, the default), properties are NOT the agent's output — they're **internal scaffolding** that guides the LLM's reasoning. Each field description tells the model what to observe and track while formulating its response.

The properties render as a `## Thinking Structure` block in the system prompt:

```
## Thinking Structure

Use these to guide your reasoning. Do NOT include these labels in output:

```yaml
user_intent: string
  # Classify: question, task, greeting, follow-up
topic: string
  # Primary topic the user is asking about
```

CRITICAL: Respond with conversational text only. Do NOT output field names, YAML, or JSON.
```

In **structured mode** (`structured_output: true`), the model MUST return a JSON object matching the properties schema. Use for background processors like DreamingAgent where output maps to database entities (Moments, GraphEdges).

### 3. Tool References

Tools are `{name, server, description}` dicts:

- **name**: Tool function name on the MCP server
- **server**: Server alias; omit or `null` for local (defaults to local)
- **description**: Optional suffix appended to the tool's base description from the MCP server, giving this agent context-specific guidance

Tools with descriptions get a `## Tool Notes` section in the system prompt:

```
## Tool Notes
- **search**: Query knowledge base using REM dialect (LOOKUP, SEARCH, FUZZY, TRAVERSE, SQL)
- **remind_me**: Create scheduled reminders — cron for recurring, ISO datetime for one-time
```

### 4. Description Stripping

When `structured_output: true`, the system prompt (from `description`) is already sent to the LLM as a system message. Pydantic converts class docstrings into JSON Schema `description` fields, which would duplicate the system prompt in the response schema. **We strip the model-level description** from the schema sent to the LLM to prevent sending instructions twice:

```python
class SchemaWrapper(result_type):
    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        schema.pop("description", None)  # Remove duplication
        return schema
```

## Defining Agents

### As Pydantic model classes

```python
from pydantic import BaseModel, Field
from p8.agentic.agent_schema import AgentSchema

class MyAgent(BaseModel):
    """You are a research assistant. Search before answering."""

    topic: str = Field(description="Primary topic of the question")
    requires_search: bool = Field(description="Whether to search first")

    model_config = {"json_schema_extra": {
        "name": "my-agent",
        "tools": [
            {"name": "search", "description": "Query KB using REM"},
            {"name": "action"},
        ],
    }}

# Convert to AgentSchema
schema = AgentSchema.from_model_class(MyAgent)
```

### As YAML files

Place in `.schema/` (or any dir via `P8_SCHEMA_DIR`):

```yaml
type: object
name: my-agent
description: |
  You are a research assistant. Search before answering.
properties:
  topic:
    type: string
    description: Primary topic of the question
tools:
  - name: search
    description: Query KB using REM
  - name: action
limits:
  request_limit: 10
  total_tokens_limit: 50000
```

### Programmatic

```python
schema = AgentSchema.build(
    name="my-agent",
    description="You are a research assistant.",
    tools=[{"name": "search", "description": "Query KB"}],
    temperature=0.3,
)
```

## Agent Lifecycle

```
Pydantic model class / YAML file / AgentSchema.build()
    ↓  AgentSchema.from_model_class() / from_yaml_file() / build()
AgentSchema (flat, unified)
    ↓  .to_schema_dict()
Schema row in DB (name, kind, content, json_schema)
    ↓  AgentAdapter.from_schema_name()  [cached 5 min per (name, user_id)]
AgentAdapter
    ↓  .build_agent()
pydantic-ai Agent (model, system_prompt, tools, limits)
    ↓  agent.run() / agent.iter()
Response + persist_turn()
```

### Loading priority

1. **Database** — Schema row with `kind='agent'`
2. **Built-in code agents** — `core_agents.py` (GeneralAgent, DreamingAgent, SampleAgent)
3. **YAML files** — from `P8_SCHEMA_DIR` (disabled by default; set e.g. `P8_SCHEMA_DIR=.schema` or any path)

YAML agents are lazy-loaded on first cache miss and auto-registered to DB.

## Tool Resolution

```python
toolsets, tools = adapter.resolve_toolsets(mcp_server=server)
# toolsets → [FastMCPToolset] — loaded from local FastMCP server, filtered to declared tools
# tools   → [ask_agent]       — delegate tools as direct Python functions
```

- Tools are grouped by `server` field and resolved via `FastMCPToolset`
- Only tools declared in the agent's `tools` list are loaded — no extras
- `ask_agent` is special: always a direct Python function (not from MCP) to avoid namespace conflicts

## Context Injection

Runtime context (date, user, session, routing) is injected via pydantic-ai's `instructions` parameter:

```python
injector = adapter.build_injector(
    user_id=user_id,
    user_email="alice@example.com",
    session_id="sess-123",
)
result = await agent.run(prompt, instructions=injector.instructions)
```

## Multi-Agent Delegation

Agents delegate via `ask_agent(agent_name, input_text)`:

1. **Parent agent** calls `ask_agent` tool
2. **Child agent** loads, runs, streams response
3. **Child events** bubble up via event sink (`asyncio.Queue` in `ContextVar`)
4. **Tool calls** from child are saved to DB for the session

Child streaming is **non-blocking** — child events are interleaved with parent events via `_merged_event_stream()`.

## Observability

### OpenTelemetry

When `P8_OTEL_ENABLED=true`, all pydantic-ai agent runs emit spans.

| Setting | Default | Description |
|---------|---------|-------------|
| `P8_OTEL_ENABLED` | `false` | Enable OTLP pipeline |
| `P8_OTEL_SERVICE_NAME` | `p8-api` | Service name |
| `P8_OTEL_COLLECTOR_ENDPOINT` | `http://localhost:4318` | OTLP collector URL |

### Per-Turn Usage Metrics

Every assistant message is stamped with usage data:

| Column | Type | Description |
|--------|------|-------------|
| `input_tokens` | `INT` | Total input tokens sent to LLM |
| `output_tokens` | `INT` | Total output tokens generated |
| `latency_ms` | `INT` | Wall-clock ms from stream start to completion |
| `model` | `VARCHAR(100)` | Provider:model string |
| `agent_name` | `VARCHAR(255)` | Schema name of the handling agent |
| `trace_id` | `VARCHAR` | OTEL trace ID |
| `span_id` | `VARCHAR` | OTEL span ID |

```sql
SELECT agent_name, model,
       COUNT(*)             AS turns,
       SUM(input_tokens)    AS total_input,
       SUM(output_tokens)   AS total_output,
       AVG(latency_ms)::int AS avg_latency_ms
FROM messages
WHERE message_type = 'assistant' AND input_tokens > 0
GROUP BY agent_name, model
ORDER BY total_input DESC;
```

## Built-in Agents

| Agent | Mode | Purpose |
|-------|------|---------|
| `GeneralAgent` | conversational | Default user-facing assistant with thinking aides (intent, topic, search strategy) |
| `DreamingAgent` | structured | Background reflective processor — outputs `DreamMoment` + `AffinityFragment` objects |
| `SampleAgent` | conversational | Minimal example for tests and docs |

## Debugging: View Actual LLM Payload

To see the exact JSON payload pydantic-ai sends to the LLM, use the `--debug` flag on the CLI:

```bash
# Conversational agent — observe Thinking Structure in system prompt, no output_tools
uv run p8 chat --agent general --debug 2>payload.log

# Structured agent — observe output_tools with final_result schema, no Thinking Structure
uv run p8 chat --agent dreaming-agent --debug 2>payload.log
```

The `--debug` flag enables `openai._base_client` DEBUG logging. The logger outputs the full `json_data` in its "Request options" log line — this is what pydantic-ai actually sends, not a reconstruction:

- **messages** array — system prompt (with `## Tool Notes` and `## Thinking Structure` for conversational agents), user prompt, conversation history
- **tools** array — only tools declared in the agent's schema; everything else on the MCP server is filtered out
- **output_tools** — present only when `structured_output: true`, contains a `final_result` tool whose parameters are the agent's output schema (with `description` stripped to avoid duplication)
- **model**, **temperature**, **stream**, **tool_choice** settings

Or in Python directly:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('openai._base_client').setLevel(logging.DEBUG)
```

### Captured example payloads

Pre-captured payloads live in [`tests/data/examples/`](../../tests/data/examples/):

| File | Agent | Key difference |
|------|-------|----------------|
| [`intercept_unstructured.yaml`](../../tests/data/examples/intercept_unstructured.yaml) | general (conversational) | `## Thinking Structure` in system prompt, no `output_tools` |
| [`intercept_structured.yaml`](../../tests/data/examples/intercept_structured.yaml) | dreaming-agent (structured) | `output_tools` with `final_result` schema, no thinking structure |

Regenerate with: `uv run python tests/data/examples/capture_payloads.py`

See also `test_function_model_captures_llm_input` in `tests/agents/test_chat.py` for the programmatic equivalent using `FunctionModel` to intercept the call in tests.

## Verification Checklist

| # | What to verify | CLI example | curl example |
|---|---------------|-------------|-------------|
| 1 | **Agents load from DB or schema folder as fallback.** Schema folder can be any path (e.g. `/tmp/schema`). | `P8_SCHEMA_DIR=/tmp/schema p8 chat --agent my-agent` | `CHAT_ID=$(uuidgen); curl -X POST "http://localhost:8000/chat/${CHAT_ID}" -H 'x-agent-schema-name: my-agent' -H 'Content-Type: application/json' -d '{"messages":[{"id":"m1","role":"user","content":"hello"}]}'` |
| 2 | **Config properties override defaults.** Agent-level `temperature`, `model`, `limits` take precedence over settings defaults. | `p8 query "SQL SELECT json_schema->>'temperature' FROM schemas WHERE name='dreaming-agent'"` | `curl http://localhost:8000/schemas/?name=dreaming-agent` — check `json_schema.temperature` is `0.7`, not settings default |
| 3 | **Streaming children are non-blocking.** Child agent events interleave with parent via `_merged_event_stream`. | `p8 chat --agent general` then ask it to delegate: "ask the sample agent about X" | `curl -N -X POST "http://localhost:8000/chat/$(uuidgen)" -H 'x-agent-schema-name: general' -H 'Content-Type: application/json' -d '{"messages":[{"id":"m1","role":"user","content":"ask the sample agent about X"}]}'` — observe `child_content` SSE events interleaved with parent events |
| 4 | **Structured response disabled adds YAML properties into prompt.** Conversational agents get a `## Thinking Structure` block. | `python -c "from p8.agentic.core_agents import GENERAL_AGENT; print(GENERAL_AGENT.get_system_prompt())"` | `curl http://localhost:8000/schemas/?name=general` — then call `AgentSchema.from_schema_row(row).get_system_prompt()` and verify `## Thinking Structure` present |
| 5 | **Structured response enabled saves tool calls in message.** Delegate runs with structured output persist tool_calls JSONB on assistant messages. | `p8 query "SQL SELECT tool_calls FROM messages WHERE session_id='<id>' AND tool_calls IS NOT NULL"` | `curl -X POST http://localhost:8000/query/ -H 'Content-Type: application/json' -d '{"mode":"SQL","query":"SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL LIMIT 5"}'` |
| 6 | **Latency and token count fields populated on metrics.** `input_tokens`, `output_tokens`, `latency_ms` are non-zero on assistant messages. | `p8 query "SQL SELECT input_tokens, output_tokens, latency_ms FROM messages WHERE message_type='assistant' AND input_tokens > 0 LIMIT 5"` | `curl -X POST http://localhost:8000/query/ -H 'Content-Type: application/json' -d '{"mode":"SQL","query":"SELECT input_tokens, output_tokens, latency_ms, model FROM messages WHERE message_type='"'"'assistant'"'"' AND input_tokens > 0 LIMIT 5"}'` |
| 7 | **Only tools declared in agent schema are sent to LLM.** Enable `openai._base_client` DEBUG logging (see above) and check the `tools` array in the request payload — it should contain only the tools listed in the agent's schema, not every tool on the MCP server. | Enable debug logging, run `p8 chat --agent sample-agent`, check "Request options" log — `tools` should be exactly `[search, action, ask_agent]` | See `test_function_model_captures_llm_input` in `test_chat.py` for programmatic verification via `FunctionModel` |
| 8 | **Structured output has description stripped.** When `structured_output: true`, the Pydantic model's `model_json_schema()` omits top-level `description`. | `python -c "from p8.agentic.core_agents import DREAMING_AGENT; M = DREAMING_AGENT.to_output_schema(); print('description' not in M.model_json_schema())"` — prints `True` | N/A — verify in unit tests: `test_build_agent_structured_output` in `test_chat.py` |
| 9 | **Agent-specific tool descriptions are appended to MCP tool descriptions.** When pydantic-ai constructs a tool from the MCP server it already has a base description. The `description` field on a tool reference in the agent schema is an extra suffix — agent-specific context appended via `## Tool Notes` in the system prompt. | `python -c "from p8.agentic.core_agents import GENERAL_AGENT; p = GENERAL_AGENT.get_system_prompt(); assert '## Tool Notes' in p; print('OK')"` | N/A — verify in unit tests: `test_tool_notes_in_system_prompt` in `test_agent_tools.py` |
