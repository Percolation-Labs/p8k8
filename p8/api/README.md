# api/

HTTP layer (FastAPI) and CLI (Typer). Both are thin — all business logic lives in `services/`.

## Getting started (cold start)

```bash
# 1. Install dependencies
uv sync

# 2. Create .env (copy from .env.example and set your OpenAI key)
cp .env.example .env
# Then edit .env and set:
#   P8_OPENAI_API_KEY=sk-...   ← REQUIRED for chat (LLM) and embeddings

# 3. Build and start Postgres + OpenBao (KMS)
docker compose up -d --build
# 4. Run migrations (blank DB — no SQL baked into the image)
p8 migrate
```

**Critical env vars:**

| Variable | Required | Why |
|----------|----------|-----|
| `P8_OPENAI_API_KEY` | Yes | Powers LLM chat (GPT-4.1) and embeddings (text-embedding-3-small) |
| `P8_DATABASE_URL` | No | Defaults to `postgresql://p8:p8_dev@localhost:5488/p8` (matches docker-compose) |
| `P8_KMS_PROVIDER` | No | Defaults to `local` (file-based master key at `.keys/.dev-master.key`) |

Everything else is optional for local dev. See `.env.example` for OAuth, push notifications, S3, Stripe, etc.

```bash
# 4. Start the API server
uv run p8 serve --reload

# 5. Verify it works
curl http://localhost:8000/health
# → {"status":"ok"}

# 6. Chat with the default agent
uv run p8 chat
# or via API (minimal — AG-UI fields are optional, defaults are filled in):
CHAT_ID=$(uuidgen)
curl -N -X POST "http://localhost:8000/chat/${CHAT_ID}" \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: general" \
  -d "{
    \"messages\": [{\"id\": \"$(uuidgen)\", \"role\": \"user\", \"content\": \"hello\"}]
  }"
```

`p8 migrate` is required after `docker compose up` to initialize the database schema.

## Starting the server

```bash
p8 serve                        # default: 0.0.0.0:8000
p8 serve --port 9000 --reload   # dev mode with auto-reload
```

Or directly via uvicorn:

```bash
uvicorn p8.api.main:app --reload
```

## Endpoints

### Chat — `POST /chat/{chat_id}`

Streaming chat with AG-UI protocol. Returns an SSE stream of typed events.

**Headers:**

| Header | Required | Description |
|--------|----------|-------------|
| `x-agent-schema-name` | No | Agent schema name (defaults to `general`; must exist in `schemas` table with `kind='agent'`) |
| `x-user-id` | No | User identity for context injection and message persistence |
| `x-user-email` | No | User email for context injection |
| `x-user-name` | No | User display name for context injection |
| `x-added-instruction` | No | Extra instruction injected into agent context (not persisted to messages) |
| `Accept` | No | `text/event-stream` (default) |

**Body:** AG-UI `RunAgentInput` — all fields optional. Defaults are filled from the URL `chat_id`. Supported: `threadId`, `runId`, `messages`, `tools`, `context`, `state`, `forwardedProps`.

#### Basic chat

```bash
CHAT_ID=$(uuidgen)

# Minimal — only messages required, everything else defaults
curl -N -X POST "http://localhost:8000/chat/${CHAT_ID}" \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: sample-agent" \
  -d "{
    \"messages\": [{\"id\": \"$(uuidgen)\", \"role\": \"user\", \"content\": \"Hello\"}]
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

#### Added instruction (ephemeral context)

The `X-Added-Instruction` header injects an instruction into the agent's context for that request only. It influences the model's response but is **never persisted** to the messages table — only the user prompt and assistant response are saved.

```bash
CHAT_ID=$(uuidgen)
curl -N -X POST "http://localhost:8000/chat/${CHAT_ID}" \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: general" \
  -H "X-Added-Instruction: Always respond in haiku form" \
  -d "{
    \"messages\": [{\"id\": \"$(uuidgen)\", \"role\": \"user\", \"content\": \"What is the weather like today?\"}]
  }"
```

The agent responds in haiku form (e.g. *"Clouds drift overhead / I cannot sense the weather / Check your local news"*) but the persisted messages contain only the user text and assistant text — no trace of the haiku instruction. See [`agentic/README.md`](../agentic/README.md#how-the-llm-payload-is-assembled) for how instructions fit into the three-layer prompt assembly.

#### Multi-agent delegation with real-time child streaming

When an agent has the `ask_agent` tool, it can delegate to other agents. Child agent
content streams token-by-token in real-time as `CUSTOM` events, interleaved with
the parent's tool execution events. This is achieved via `agent.iter()` + an
`asyncio.Queue` event sink + `asyncio.wait(FIRST_COMPLETED)` multiplexing.

First, register a child agent if you dont have one already you want to use:

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
  -d "{
    \"messages\": [{
      \"id\": \"$(uuidgen)\",
      \"role\": \"user\",
      \"content\": \"Use the ask_agent tool to delegate to echo-child: say hello\"
    }]
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
Client <-SSE- StreamingResponse <- _merged_event_stream()
                                    |-- AGUIAdapter.run_stream()  -> AG-UI events (parent)
                                    +-- child_event_sink (Queue)  -> CustomEvent (child)
                                        via asyncio.wait(FIRST_COMPLETED)

ask_agent() [called by parent during tool execution]:
    |-- get_child_event_sink()           # ContextVar: reads the Queue
    +-- agent.iter(prompt)               # child agent (pydantic-ai)
          |-- ModelRequestNode.stream()  -> PartDeltaEvent
          |     -> queue.put({"type":"child_content", ...})   # real-time!
          +-- CallToolsNode.stream()     -> FunctionToolCallEvent
                -> queue.put({"type":"child_tool_start/result", ...})
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

### Content — `/content/`

```bash
# Upload a file for extraction + chunking (inline or queued based on size)
curl -X POST http://localhost:8000/content/ \
  -F "file=@document.pdf" \
  -F "category=docs"

# Download a file by ID
curl http://localhost:8000/content/files/{file_id} -o output.pdf
```

### Moments — `/moments/`

```bash
# Paginated feed with virtual daily summaries
curl "http://localhost:8000/moments/feed?limit=20"
curl "http://localhost:8000/moments/feed?before_date=2025-02-18"

# Today's summary
curl http://localhost:8000/moments/today

# Session timeline (interleaved messages + moments)
curl http://localhost:8000/moments/session/{session_id}

# Get single moment
curl http://localhost:8000/moments/{moment_id}

# List with filters
curl "http://localhost:8000/moments/?moment_type=session_chunk&limit=50"
```

### Auth — `/auth/`

```bash
# Create tenant
curl -X POST http://localhost:8000/auth/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "acme", "encryption_mode": "platform"}'

# Get tenant
curl http://localhost:8000/auth/tenants/{tenant_id}

# Configure encryption mode
curl -X POST http://localhost:8000/auth/tenants/{tenant_id}/encryption \
  -H "Content-Type: application/json" \
  -d '{"mode": "client"}'

# Create user under tenant
curl -X POST http://localhost:8000/auth/tenants/{tenant_id}/users \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice", "email": "alice@acme.com"}'

# List users in tenant
curl http://localhost:8000/auth/tenants/{tenant_id}/users

# One-step signup (personal tenant + user)
curl -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice", "email": "alice@example.com"}'

# OAuth — redirect to provider
curl http://localhost:8000/auth/authorize?provider=google
curl http://localhost:8000/auth/authorize?provider=apple

# Magic link (always 200 — no email leak)
curl -X POST http://localhost:8000/auth/magic-link \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com"}'

# Token refresh (from body or cookie)
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "..."}'

# Revoke refresh token
curl -X POST http://localhost:8000/auth/revoke \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "..."}'

# Current user profile (JWT required)
curl http://localhost:8000/auth/me -H "Authorization: Bearer ..."

# Update profile (register device tokens, change name, etc.)
curl -X PATCH http://localhost:8000/auth/me \
  -H "Authorization: Bearer ..." \
  -H "Content-Type: application/json" \
  -d '{"devices": [{"platform": "apns", "token": "...", "device_name": "iPhone"}]}'

# Logout (clears cookies, revokes refresh)
curl -X POST http://localhost:8000/auth/logout
```

Mobile OAuth (browser-based, redirects to `remapp://` deep link):

| Endpoint | Description |
|----------|-------------|
| `GET /auth/mobile/authorize/{provider}` | Initiate OAuth from mobile app |
| `GET/POST /auth/mobile/callback/{provider}` | Handle callback, redirect to app with tokens |
| `GET /auth/mobile/authorize/google-drive` | Initiate Google Drive scope OAuth |
| `GET/POST /auth/mobile/callback/google-drive` | Store Drive refresh token |
| `POST /auth/mobile/google/drive-disconnect` | Revoke stored Drive grant |
| `GET /auth/mobile/google/drive-status` | Check if user has active Drive grant |

### Billing — `/billing/`

Stripe integration (JWT auth, no API key). Only available when `P8_STRIPE_SECRET_KEY` is set.

```bash
# Subscription status
curl http://localhost:8000/billing/subscription -H "Authorization: Bearer ..."

# Usage across metered resources
curl http://localhost:8000/billing/usage -H "Authorization: Bearer ..."

# Create checkout session
curl -X POST http://localhost:8000/billing/checkout \
  -H "Authorization: Bearer ..." \
  -H "Content-Type: application/json" \
  -d '{"plan_id": "pro"}'

# Create add-on checkout (e.g. chat token pack)
curl -X POST http://localhost:8000/billing/addon \
  -H "Authorization: Bearer ..." \
  -H "Content-Type: application/json" \
  -d '{"addon_id": "chat_tokens_50k"}'

# Billing portal
curl -X POST http://localhost:8000/billing/portal -H "Authorization: Bearer ..."

# Stripe webhook (signature-verified, no auth)
# POST /billing/webhooks — called by Stripe
```

### Share — `/share/`

Share moments between users via graph_edges.

```bash
# Share a moment with a user
curl -X POST http://localhost:8000/share/ \
  -H "Content-Type: application/json" \
  -d '{"moment_id": "...", "target_user_id": "..."}'

# Unshare
curl -X DELETE http://localhost:8000/share/ \
  -H "Content-Type: application/json" \
  -d '{"moment_id": "...", "target_user_id": "..."}'

# List who a moment is shared with
curl http://localhost:8000/share/moment/{moment_id}

# List moments shared with me
curl "http://localhost:8000/share/with-me?user_id=..."
```

### Notifications — `/notifications/`

```bash
# Send push notification to users (called by pg_cron reminders via pg_net)
curl -X POST http://localhost:8000/notifications/send \
  -H "Content-Type: application/json" \
  -d '{"user_ids": ["..."], "title": "Reminder", "body": "Take your vitamins"}'
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

### Admin — `/admin/`

```bash
curl http://localhost:8000/admin/health
curl -X POST http://localhost:8000/admin/rebuild-kv
curl http://localhost:8000/admin/queue
curl http://localhost:8000/admin/queue/stats
```

### MCP — `/mcp` (HTTP) / `p8 mcp` (stdio)

FastMCP server exposing p8 tools and resources. Two transports:

| Transport | Endpoint | Use case |
|-----------|----------|----------|
| Streamable HTTP | `/mcp` (mounted on FastAPI) | Production, remote clients |
| stdio | `p8 mcp` | Local dev, Claude Code, IDE integrations |

**Tools:** `search`, `action`, `ask_agent`, `remind_me`, `save_moments`, `get_moments`
**Resources:** `user://profile/{user_id}`

Google OAuth enabled when `P8_GOOGLE_CLIENT_ID` is configured and `P8_MCP_AUTH_ENABLED=true` (default).

#### Local MCP setup (Claude Code / Cursor / etc.)

1. Make sure Postgres is running:

```bash
docker compose up -d --build
```

2. Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "p8": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "p8", "mcp"],
      "env": {
        "P8_MCP_AUTH_ENABLED": "false"
      }
    }
  }
}
```

Setting `P8_MCP_AUTH_ENABLED=false` disables OAuth and automatically binds tools to the
Jamie Rivera test user (`user1@example.com`). This means all tool calls (search, get_moments,
save_moments, etc.) operate in that user's scope without needing to pass `user_id` explicitly.

3. Restart your MCP client (e.g. `/mcp` in Claude Code) to pick up the config.

#### Seeding test data

The Jamie Rivera fixture provides a complete 7-day dataset (sessions, messages, moments, files):

```bash
uv run python tests/data/fixtures/jamie_rivera/seed.py --mode db
```

#### Testing tools

Once connected, test each tool:

```
# REM queries
search("FUZZY p8")
search("LOOKUP jamie-caching-adr-note")
search("SEARCH machine learning FROM ontologies LIMIT 5")

# Moments
get_moments(limit=5)
get_moments(moment_type="voice_note", limit=3)
save_moments(moments=[{"name": "test-moment", "summary": "A test", "topic_tags": ["test"]}])

# Events
action(type="observation", payload={"confidence": 0.9, "reasoning": "test"})

# Agent delegation
ask_agent(agent_name="general", input_text="hello")

# Reminders (requires pg_cron)
remind_me(name="test", description="Test reminder", crontab="0 9 * * *")
```

Encrypted fields (e.g. moment summaries) will appear as base64 ciphertext in results —
this confirms the encryption pipeline is working correctly.

### Health — `GET /health`

Root health check (matches Dockerfile HEALTHCHECK path). No auth required.

## API / CLI parity

Every operation is available through both interfaces. Both call the same services.

| Operation | API | CLI |
|-----------|-----|-----|
| REM query | `POST /query/raw` | `p8 query '<query>'` |
| Structured query | `POST /query/` | `p8 query '<query>'` |
| Schema list | `GET /schemas/` | `p8 schema list` |
| Schema get | `GET /schemas/{id}` | `p8 schema get <id>` |
| Schema upsert | `POST /schemas/` | `p8 upsert schemas <file>` |
| Schema delete | `DELETE /schemas/{id}` | `p8 schema delete <id>` |
| Schema verify | — | `p8 schema verify` |
| Schema register | — | `p8 schema register` |
| Bulk upsert | `POST /schemas/` (per item) | `p8 upsert <table> <file>` |
| Markdown ingest | — | `p8 upsert <file.md>` |
| Content upload | `POST /content/` | `p8 upsert resources <file>` |
| File download | `GET /content/files/{id}` | — (use API) |
| Moments feed | `GET /moments/feed` | `p8 moments` |
| Session timeline | `GET /moments/session/{id}` | `p8 moments timeline <id>` |
| Moment compaction | — | `p8 moments compact <id>` |
| Chat | `POST /chat/{id}` | `p8 chat [--agent name] [--debug]` |
| Encryption status | `GET /auth/tenants/{id}` | `p8 encryption status` |
| Encryption config | `POST /auth/tenants/{id}/encryption` | `p8 encryption configure <id>` |
| Encryption test | — | `p8 encryption test` |
| Embed queue | `POST /embeddings/process` | — (use API) |
| Embed generate | `POST /embeddings/generate` | — (use API) |
| MCP server | `/mcp` (Streamable HTTP) | `p8 mcp` (stdio) |
| Migrate | — | `p8 migrate` |
| Serve | `uvicorn p8.api.main:app` | `p8 serve` |
| Health | `GET /admin/health` | — (use API) |
| Rebuild KV | `POST /admin/rebuild-kv` | — (use API) |

## Architecture

```
api/
├── main.py             # FastAPI app factory + lifespan (delegates to services/bootstrap.py)
├── deps.py             # FastAPI Depends() helpers (get_db, get_encryption, get_current_user)
├── mcp_server.py       # FastMCP server: search, action, ask_agent, remind_me, save_moments, get_moments
├── controllers/
│   └── chat.py         # ChatController — shared logic for API + CLI chat
├── cli/                # Typer CLI — thin wrappers over services
│   ├── __init__.py     # App + subcommand registration
│   ├── __main__.py     # python -m p8.api.cli entry point
│   ├── serve.py        # p8 serve
│   ├── migrate.py      # p8 migrate
│   ├── query.py        # p8 query
│   ├── upsert.py       # p8 upsert
│   ├── schema.py       # p8 schema list/get/delete/verify/register
│   ├── chat.py         # p8 chat [--agent] [--debug]
│   ├── moments.py      # p8 moments / p8 moments timeline / p8 moments compact
│   ├── encryption.py   # p8 encryption status/configure/test/test-isolation
│   └── mcp.py          # p8 mcp (stdio transport)
├── routers/            # FastAPI routers — thin wrappers over services
│   ├── chat.py         # /chat/{chat_id} — AG-UI streaming + child delegation
│   ├── query.py        # /query/, /query/raw
│   ├── schemas.py      # /schemas/ CRUD
│   ├── content.py      # /content/ file upload + /content/files/{id} download
│   ├── moments.py      # /moments/ feed, today, timeline, list, get
│   ├── auth.py         # /auth/ tenants, users, OAuth, magic link, sessions
│   ├── payments.py     # /billing/ Stripe subscription, checkout, portal, webhooks
│   ├── share.py        # /share/ moment sharing via graph_edges
│   ├── notifications.py # /notifications/ push notification relay
│   ├── admin.py        # /admin/ health, rebuild-kv, queue, queue/stats
│   └── embeddings.py   # /embeddings/ process & generate
└── tools/              # MCP tool implementations
    ├── __init__.py     # Module-level state (init_tools)
    ├── search.py       # search — REM query execution
    ├── action.py       # action — typed event emission
    ├── ask_agent.py    # ask_agent — multi-agent delegation
    ├── remind_me.py    # remind_me — scheduled push reminders via pg_cron
    ├── save_moments.py # save_moments — create moments
    └── get_moments.py  # get_moments — retrieve moments
```

Both `cli/` and `routers/` are thin. All logic lives in `services/`.
