# Agentic

Agent orchestration in p8. Agents are declarative schema rows — system prompt in `content`, config in `json_schema`, runtime via `AgentAdapter`.

## Agent Schema

An agent is a flat YAML/JSON doc with two groups of fields:

**JSON Schema standard:** `type`, `description`, `properties`, `required`
**Agent config:** `name`, `tools`, `model`, `temperature`, `limits`, `structured_output`, `chained_tool`, ...

```yaml
type: object
name: my-agent
description: |
  You are a helpful assistant...
properties:
  topic:
    type: string
    description: Primary topic
tools:
  - name: search
    description: Query knowledge base
structured_output: false
```

### Output Modes

**Conversational** (`structured_output: false`, default): Agent returns free-form text. Properties are "thinking aides" — internal scaffolding that guides the LLM's reasoning.

**Structured** (`structured_output: true`): Agent MUST return a JSON object matching the properties schema. Used for background processors (dreaming, classification, extraction) where output maps to data.

## Chained Tool Calls

When an agent produces structured output, you often want to automatically invoke a follow-up tool with that output — persist moments, save records, trigger actions — without a second LLM turn.

The `chained_tool` field makes this declarative:

```yaml
type: object
name: invoice-agent
description: Extract invoice data from documents.
structured_output: true
chained_tool: save_moments    # auto-invoke after structured output
properties:
  moments:
    type: array
    description: Extracted invoice records
tools:
  - name: search
```

### How It Works

1. Agent runs and produces structured output (e.g. `{"moments": [...]}`)
2. System looks up `chained_tool` name in the **tool registry** — a map of tool names to their Python callables (the same functions registered on the MCP server)
3. Tool is called with the structured output dict as keyword arguments
4. Both the tool call and its result are persisted as `tool_call` + `tool_response` message pairs in the session
5. The chained tool result is returned alongside the agent output

### Guard Rails

- **Both fields required**: Chaining only fires when `structured_output: true` AND `chained_tool` is set
- **Missing tool**: If the named tool isn't in the registry, a warning is logged and the agent's output is still returned intact
- **Error isolation**: If the chained tool raises an exception, it's caught and logged. The agent's structured output is never lost
- **No extra LLM turn**: The tool is called directly as a Python function — no MCP protocol overhead, no additional model inference

### Where Chaining Runs

Chaining is invoked in three places, all after the agent run completes:

| Path | Location | Notes |
|------|----------|-------|
| `ask_agent` (delegation) | `p8/api/tools/ask_agent.py` | Both streaming and non-streaming |
| `ChatController.run_turn()` | `p8/api/controllers/chat.py` | Synchronous chat |
| `ChatController.run_turn_stream()` | `p8/api/controllers/chat.py` | Streaming chat |

### Tool Registry

The registry maps tool names to async callables. It's built lazily from the same functions imported in `mcp_server.py`:

```python
from p8.api.tools import get_tool_fn

fn = get_tool_fn("save_moments")  # returns the callable or None
```

Available tools: `search`, `action`, `ask_agent`, `save_moments`, `get_moments`, `web_search`, `update_user_metadata`, `remind_me`.

### Example: Dreaming Agent (Future)

The dreaming handler currently has ~90 lines of bespoke persistence code in `_persist_dream_moments`. With chained tools, the dreaming agent could declare:

```yaml
structured_output: true
chained_tool: save_moments
```

And the system would automatically call `save_moments(dream_moments=[...], ...)` with the agent's structured output. The handler would only need to run the agent — persistence becomes declarative.

### Session Message Format

When a chained tool executes, two messages are persisted to the session:

```
tool_call:     {name: "save_moments", id: "<uuid>", arguments: {<structured_output>}}
tool_response: {name: "save_moments", id: "<uuid>"}  + content = JSON result
```

This gives full observability — you can see exactly what the tool received and returned by querying session messages.

## Key Files

| File | Role |
|------|------|
| `p8/agentic/agent_schema.py` | `AgentSchema` — flat schema with `chained_tool` field |
| `p8/agentic/adapter.py` | `AgentAdapter` — `execute_chained_tool()` method |
| `p8/agentic/core_agents.py` | Built-in agent definitions (GeneralAgent, DreamingAgent, SampleAgent) |
| `p8/api/tools/__init__.py` | Tool registry (`get_tool_fn()`) |
| `p8/api/tools/ask_agent.py` | Delegation with chaining support |
| `p8/api/controllers/chat.py` | Chat controller with chaining support |
