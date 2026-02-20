# api/

HTTP layer (FastAPI) and CLI (Typer). Both are thin — all business logic lives in `services/`.

## Starting the server

```bash
p8 serve                        # default: 0.0.0.0:8000
p8 serve --port 9000 --reload   # dev mode with auto-reload
```

Or directly via uvicorn:

```bash
uvicorn api.main:app --reload
```

## Endpoints

### Chat — `POST /chat/{chat_id}`

Streaming chat with AG-UI protocol. Returns an SSE stream of typed events.

**Headers:**

| Header | Required | Description |
|--------|----------|-------------|
| `x-agent-schema-name` | Yes | Agent schema name (must exist in `schemas` table with `kind='agent'`) |
| `x-user-id` | No | User identity for context injection and message persistence |
| `x-user-email` | No | User email for context injection |
| `x-user-name` | No | User display name for context injection |
| `Accept` | No | `text/event-stream` (default) |

**Body:** AG-UI `RunAgentInput` — `thread_id`, `run_id`, `messages`, `tools`, `context`, `state`.

#### Basic chat

```bash
CHAT_ID=$(uuidgen)

curl -N -X POST "http://localhost:8000/chat/${CHAT_ID}" \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: sample-agent" \
  -H "Accept: text/event-stream" \
  -d "{
    \"thread_id\": \"${CHAT_ID}\",
    \"run_id\": \"$(uuidgen)\",
    \"state\": {},
    \"messages\": [{\"id\": \"$(uuidgen)\", \"role\": \"user\", \"content\": \"Hello\"}],
    \"tools\": [], \"context\": [], \"forwarded_props\": {}
  }"
```

SSE stream:

```
data: {"type":"RUN_STARTED", ...}
data: {"type":"TEXT_MESSAGE_START", "messageId":"...", "role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT", "delta":"Hello"}
data: {"type":"TEXT_MESSAGE_CONTENT", "delta":"! How can I help?"}
data: {"type":"TEXT_MESSAGE_END", ...}
data: {"type":"RUN_FINISHED", ...}
```

#### Multi-agent delegation with real-time child streaming

When an agent has the `ask_agent` tool, it can delegate to other agents. Child agent
content streams token-by-token in real-time as `CUSTOM` events, interleaved with
the parent's tool execution events. This is achieved via `agent.iter()` + an
`asyncio.Queue` event sink + `asyncio.wait(FIRST_COMPLETED)` multiplexing.

First, register a child agent:

```bash
curl -X POST http://localhost:8000/schemas/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "echo-child",
    "kind": "agent",
    "description": "A simple echo agent",
    "content": "You are a helpful echo agent. Repeat back what the user says with elaboration."
  }'
```

Then chat with the parent (sample-agent has `ask_agent` in its tools):

```bash
CHAT_ID=$(uuidgen)

curl -N -X POST "http://localhost:8000/chat/${CHAT_ID}" \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: sample-agent" \
  -H "Accept: text/event-stream" \
  -d "{
    \"thread_id\": \"${CHAT_ID}\",
    \"run_id\": \"$(uuidgen)\",
    \"state\": {},
    \"messages\": [{
      \"id\": \"$(uuidgen)\",
      \"role\": \"user\",
      \"content\": \"Use the ask_agent tool to delegate to echo-child: say hello\"
    }],
    \"tools\": [], \"context\": [], \"forwarded_props\": {}
  }"
```

SSE stream with delegation:

```
data: {"type":"RUN_STARTED", ...}

# 1. Parent decides to call ask_agent
data: {"type":"TOOL_CALL_START", "toolCallId":"...", "toolCallName":"ask_agent", ...}
data: {"type":"TOOL_CALL_ARGS", "delta":"{\"agent_name\": \"echo-child\", ...}"}
data: {"type":"TOOL_CALL_END", ...}

# 2. Child content streams in real-time (token-by-token, DURING tool execution)
data: {"type":"CUSTOM", "name":"child_content", "value":{"type":"child_content","agent_name":"echo-child","content":"Hello"}}
data: {"type":"CUSTOM", "name":"child_content", "value":{"type":"child_content","agent_name":"echo-child","content":"! It's"}}
data: {"type":"CUSTOM", "name":"child_content", "value":{"type":"child_content","agent_name":"echo-child","content":" wonderful to"}}
data: {"type":"CUSTOM", "name":"child_content", "value":{"type":"child_content","agent_name":"echo-child","content":" hear from you."}}
...

# 3. Tool result with full child response
data: {"type":"TOOL_CALL_RESULT", "content":"{\"status\":\"success\", ...}", ...}

# 4. Parent summarizes
data: {"type":"TEXT_MESSAGE_START", ...}
data: {"type":"TEXT_MESSAGE_CONTENT", "delta":"The echo-child agent responded: ..."}
data: {"type":"TEXT_MESSAGE_END", ...}
data: {"type":"RUN_FINISHED", ...}
```

**Child event types** (all `CUSTOM` events with `value.agent_name` for client-side routing):

| Event name | Description | Value fields |
|-----------|-------------|--------------|
| `child_content` | Text content token from child agent | `agent_name`, `content` |
| `child_tool_start` | Child agent called a tool | `agent_name`, `tool_name`, `tool_call_id`, `arguments` |
| `child_tool_result` | Child agent received tool result | `agent_name`, `tool_name`, `tool_call_id`, `result` |

**Architecture:**

```
Client ←SSE— StreamingResponse ← _merged_event_stream()
                                    ├── AGUIAdapter.run_stream()  → AG-UI events (parent)
                                    └── child_event_sink (Queue)  → CustomEvent (child)
                                        via asyncio.wait(FIRST_COMPLETED)

ask_agent() [called by parent during tool execution]:
    ├── get_child_event_sink()           # ContextVar: reads the Queue
    └── agent.iter(prompt)               # child agent (pydantic-ai)
          ├── ModelRequestNode.stream()  → PartDeltaEvent
          │     → queue.put({"type":"child_content", ...})   # real-time!
          └── CallToolsNode.stream()     → FunctionToolCallEvent
                → queue.put({"type":"child_tool_start/result", ...})
```

Key: child events arrive **during** tool execution (not buffered until after). The
multiplexer uses `asyncio.wait(FIRST_COMPLETED)` to race the parent's AG-UI event
stream against the child's event queue, yielding whichever completes first.

### Query — `POST /query/`

Structured REM query with explicit mode.

```bash
curl -X POST http://localhost:8000/query/ \
  -H "Content-Type: application/json" \
  -d '{"mode": "LOOKUP", "key": "sarah-chen"}'
```

### Query (raw) — `POST /query/raw`

REM dialect string — the same syntax the CLI uses.

```bash
curl -X POST http://localhost:8000/query/raw \
  -H "Content-Type: application/json" \
  -d '{"query": "LOOKUP \"sarah-chen\""}'

curl -X POST http://localhost:8000/query/raw \
  -d '{"query": "SEARCH \"database\" FROM schemas LIMIT 5"}'

curl -X POST http://localhost:8000/query/raw \
  -d '{"query": "FUZZY \"sara\" LIMIT 10"}'

curl -X POST http://localhost:8000/query/raw \
  -d '{"query": "SELECT name, kind FROM schemas LIMIT 5"}'
```

### Schemas — `/schemas/`

```bash
# Create / update
curl -X POST http://localhost:8000/schemas/ \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent", "kind": "agent", "description": "A helpful agent"}'

# List (optional ?kind=agent filter)
curl http://localhost:8000/schemas/
curl http://localhost:8000/schemas/?kind=agent

# Get by ID
curl http://localhost:8000/schemas/{schema_id}

# Delete (soft)
curl -X DELETE http://localhost:8000/schemas/{schema_id}
```

### Content — `POST /content/`

```bash
# Upload a file for extraction + chunking
curl -X POST http://localhost:8000/content/ \
  -F "file=@document.pdf" \
  -F "category=docs"
```

### Embeddings — `/embeddings/`

```bash
# Process one batch from the embedding queue
curl -X POST http://localhost:8000/embeddings/process

# Generate embeddings for arbitrary texts
curl -X POST http://localhost:8000/embeddings/generate \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world", "database migration"]}'
```

### Auth — `/auth/`

```bash
# Create tenant
curl -X POST http://localhost:8000/auth/tenants \
  -d '{"name": "acme", "encryption_mode": "platform"}'

# Create user under tenant
curl -X POST http://localhost:8000/auth/tenants/{tenant_id}/users \
  -d '{"name": "Alice", "email": "alice@acme.com"}'

# One-step signup (personal tenant + user)
curl -X POST http://localhost:8000/auth/signup \
  -d '{"name": "Alice", "email": "alice@example.com"}'
```

### Admin — `/admin/`

```bash
curl http://localhost:8000/admin/health
curl -X POST http://localhost:8000/admin/rebuild-kv
curl http://localhost:8000/admin/queue
```

### MCP — `/mcp`

FastMCP server (Streamable HTTP). Tools: `search`, `action`, `ask_agent`. Resource: `user://profile/{user_id}`.

## API / CLI parity

Every operation is available through both interfaces. Both call the same services.

| Operation | API | CLI |
|-----------|-----|-----|
| REM query | `POST /query/raw` | `p8 query '<query>'` |
| Structured query | `POST /query/` | `p8 query '<query>'` (parsed by RemQueryEngine) |
| Schema list | `GET /schemas/` | `p8 schema list` |
| Schema get | `GET /schemas/{id}` | `p8 schema get <id>` |
| Schema upsert | `POST /schemas/` | `p8 upsert schemas <file>` |
| Schema delete | `DELETE /schemas/{id}` | `p8 schema delete <id>` |
| Schema verify | — | `p8 schema verify` |
| Schema register | — | `p8 schema register` |
| Bulk upsert | `POST /schemas/` (per item) | `p8 upsert <table> <file>` |
| Markdown ingest | — | `p8 upsert <file.md>` |
| Content upload | `POST /content/` | `p8 upsert resources <file>` |
| Embed queue | `POST /embeddings/process` | — (use API) |
| Embed generate | `POST /embeddings/generate` | — (use API) |
| Chat | `POST /chat/{id}` | `p8 chat` |
| Migrate | — | `p8 migrate` |
| Serve | `uvicorn api.main:app` | `p8 serve` |
| Health | `GET /admin/health` | — (use API) |
| Rebuild KV | `POST /admin/rebuild-kv` | — (use API) |

## Architecture

```
api/
├── main.py             # FastAPI app factory + lifespan (delegates to services/bootstrap.py)
├── deps.py             # FastAPI Depends() helpers (get_db, get_encryption)
├── mcp_server.py       # FastMCP server: search, action, ask_agent
├── controllers/
│   └── chat.py         # ChatController — shared logic for API + CLI chat
├── cli/                # Typer CLI — thin wrappers over services
│   ├── __init__.py     # App + subcommand registration
│   ├── serve.py        # p8 serve
│   ├── migrate.py      # p8 migrate
│   ├── query.py        # p8 query
│   ├── upsert.py       # p8 upsert
│   ├── schema.py       # p8 schema list/get/delete/verify/register
│   └── chat.py         # p8 chat
├── routers/            # FastAPI routers — thin wrappers over services
│   ├── chat.py         # /chat/{chat_id}
│   ├── query.py        # /query/, /query/raw
│   ├── schemas.py      # /schemas/ CRUD
│   ├── content.py      # /content/ file upload
│   ├── admin.py        # /admin/health, rebuild-kv, queue
│   ├── auth.py         # /auth/ tenant & user management
│   └── embeddings.py   # /embeddings/ process & generate
└── tools/              # MCP tool implementations
    ├── __init__.py     # Module-level state (init_tools)
    ├── search.py       # search tool
    ├── action.py       # action tool
    └── ask_agent.py    # ask_agent tool (delegation)
```

Both `cli/` and `routers/` are thin. All logic lives in `services/`.
