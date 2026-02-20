# p8k8

p8k8 is a cloud native agentic framework with emphasis on building memory into AI systems. It leans heavily on Postgres to provide search, memory and other services and adds a declarative agent framework that wraps FastMCP and Pydantic-AI. It supports building custom ontologies and agent workflows through simple yaml and markdown documents.

It includes a deployable K8s stack for either a Hetzner or AWS cluster.


## Quick Start (Local Dev)

```bash
# Install dependencies
uv sync

# Start local postgres + KMS
docker compose up -d

# Run migrations
P8_DATABASE_URL=postgresql://p8:p8_dev@localhost:5488/p8 uv run p8 migrate

# Start API server
P8_DATABASE_URL=postgresql://p8:p8_dev@localhost:5488/p8 uv run p8 serve
```

The API is at `http://localhost:8000`. MCP server at `/mcp`.

## Deploy to Hetzner

```bash
# Build and push container image
docker buildx build --platform linux/amd64 \
  -t percolationlabs/p8:latest --push -f Dockerfile .

# Create namespace and secrets (edit secrets.yaml first!)
kubectl --context=p8-w-1 apply -f manifests/application/p8-stack/overlays/hetzner/namespace.yaml
kubectl --context=p8-w-1 apply -f manifests/application/p8-stack/overlays/hetzner/secrets.yaml

# Generate postgres init ConfigMap from SQL files
kubectl --context=p8-w-1 create configmap p8-postgres-init-sql \
  --from-file=install_entities.sql=sql/install_entities.sql \
  --from-file=install.sql=sql/install.sql \
  -n p8

# Deploy full stack
kubectl --context=p8-w-1 apply -k manifests/application/p8-stack/overlays/hetzner/
```

## CLI

The `p8` CLI provides direct access to all framework capabilities — querying the knowledge base, chatting with agents, managing content, and administering the system.

### `p8 serve` — Start the API server

```bash
p8 serve                          # default: 0.0.0.0:8000
p8 serve --port 9000 --reload     # dev mode with auto-reload
p8 serve --workers 4              # multi-worker production
```

### `p8 migrate` — Run database migrations

Executes SQL scripts in order: `install_entities.sql` → `install.sql` → `payments.sql`.

```bash
p8 migrate
```

### `p8 query` — REM query engine

Execute REM (Resource-Entity-Moment) queries against the knowledge base. Supports one-shot mode or an interactive REPL.

```bash
# One-shot queries
p8 query 'LOOKUP "demo-project-planning"'
p8 query 'LOOKUP "agent-a", "agent-b"'
p8 query 'SEARCH "database migration" FROM resources LIMIT 10'
p8 query 'FUZZY "knowledge graph" THRESHOLD 0.4 LIMIT 5'
p8 query 'TRAVERSE "my-entity" DEPTH 2 TYPE related'
p8 query 'SQL SELECT name, kind FROM schemas WHERE kind = $$agent$$'

# Table output
p8 query --format table 'LOOKUP "my-agent"'

# Scoped queries
p8 query --tenant-id acme 'SEARCH "billing"'

# Interactive REPL
p8 query
rem> LOOKUP "demo-project-planning"
rem> SEARCH "embeddings" FROM ontologies MIN_SIMILARITY 0.8
rem> \q
```

**REM query modes:**

| Mode | Syntax | Description |
|------|--------|-------------|
| LOOKUP | `LOOKUP "entity-name"` | O(1) key-value lookup by entity name |
| SEARCH | `SEARCH "query" FROM table [LIMIT n]` | Semantic vector search via pgvector |
| FUZZY | `FUZZY "text" [THRESHOLD 0.3] [LIMIT 10]` | Trigram fuzzy matching via pg_trgm |
| TRAVERSE | `TRAVERSE "key" [DEPTH n] [TYPE rel]` | Graph walk via `graph_edges` JSONB |
| SQL | `SQL SELECT ...` | Direct Postgres SQL (read-only, destructive statements blocked) |

### `p8 chat` — Interactive agent chat

Start a chat session with any agent defined in the schemas table. Uses the same `ChatController` as the API.

```bash
p8 chat                                      # new session, default "general" agent
p8 chat --agent query-agent                  # use a specific agent
p8 chat 550e8400-e29b-41d4-a716-446655440000 # resume an existing session
p8 chat --agent support --user-id <UUID>     # with user context
```

The REPL loads full conversation history each turn and supports moment compaction for long-running sessions. Type `exit`, `quit`, or `\q` to leave.

### `p8 upsert` — Load content into the knowledge base

Ingest files, directories, YAML/JSON schema definitions, or markdown documents.

```bash
# Structured data → target table
p8 upsert schemas data/agents.yaml
p8 upsert servers data/servers.yaml
p8 upsert resources data/chunks.json

# Markdown → ontologies (default) or explicit table
p8 upsert docs/architecture.md
p8 upsert ontologies docs/architecture.md

# Files → extract + chunk → File + Resource entities
p8 upsert resources paper.pdf
p8 upsert resources docs/                   # ingest entire directory

# With tenant scoping
p8 upsert schemas data/agents.yaml --tenant-id acme
```

**Valid tables:** `schemas`, `ontologies`, `resources`, `moments`, `sessions`, `messages`, `servers`, `tools`, `users`, `files`, `feedback`, `storage_grants`, `tenants`

### `p8 schema` — Manage the ontology registry

```bash
p8 schema list                    # list all schemas
p8 schema list --kind agent       # filter by kind
p8 schema list --kind model --limit 100
p8 schema get <UUID>              # full JSON for one schema
p8 schema delete <UUID>           # soft-delete
p8 schema verify                  # check DB matches Pydantic models
p8 schema register                # sync Python models → schemas table
```

### `p8 moments` — Temporal memory management

```bash
p8 moments                        # list today's moments
p8 moments --type session_chunk   # filter by moment type
p8 moments --user-id <UUID>

# Session timeline — interleaved messages + moments
p8 moments timeline <SESSION_ID>
p8 moments timeline <SESSION_ID> --limit 100

# Manual compaction — build a summary moment from recent messages
p8 moments compact <SESSION_ID>
p8 moments compact <SESSION_ID> --threshold 500
```

## API

### Chat Completions — AG-UI Streaming

The chat endpoint at `POST /chat/{session_id}` is the primary interface for connecting any client to a p8 agent. It implements the [AG-UI](https://github.com/ag-ui-protocol/ag-ui) streaming protocol, making it compatible with any AG-UI client (React, Swift, etc.).

**How it works:**

1. **Agent loading via header** — pass `x-agent-schema-name` to select any agent registered in the `schemas` table. Agents are declarative: the `content` field is the system prompt, `json_schema` holds config (model, tools, limits). No code changes needed to swap agents.
2. **Session management** — the `{session_id}` path parameter creates or resumes a session. Conversation history, moments, and context are loaded automatically.
3. **AG-UI event stream** — the response is a Server-Sent Events stream of AG-UI events (`TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`, tool calls, etc.). Any AG-UI-compatible frontend can consume this directly.
4. **Child agent multiplexing** — when an agent delegates to another agent via `ask_agent`, both parent and child events are interleaved in a single stream via `CustomEvent`, enabling real-time visibility into delegation chains.

**Headers:**

| Header | Required | Description |
|--------|----------|-------------|
| `x-agent-schema-name` | No (default: `general`) | Agent to use — any `kind='agent'` row in schemas |
| `x-user-id` | No | User UUID for context, history scoping, and quota |
| `x-user-email` | No | User email for context injection |
| `x-user-name` | No | Display name for context injection |
| `x-session-name` | No | Human-readable session name (upserted on create/update) |
| `x-session-type` | No | Session mode: `chat`, `workflow`, or `eval` |

**curl examples:**

```bash
# Simple chat — stream a response from the default agent
curl -N -X POST http://localhost:8000/chat/my-session-1 \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: general" \
  -d '{
    "messages": [{"role": "user", "content": "What do you know about me?"}]
  }'

# Use a custom agent with user context
curl -N -X POST http://localhost:8000/chat/my-session-2 \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: query-agent" \
  -H "x-user-id: 550e8400-e29b-41d4-a716-446655440000" \
  -H "x-user-email: user@example.com" \
  -d '{
    "messages": [{"role": "user", "content": "Search for recent meeting notes"}]
  }'

# Resume an existing session (pass session UUID as path)
curl -N -X POST http://localhost:8000/chat/550e8400-e29b-41d4-a716-446655440000 \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: general" \
  -d '{
    "messages": [
      {"role": "user", "content": "What did we discuss earlier?"}
    ]
  }'
```

The `-N` flag disables curl buffering so you see AG-UI events as they stream.

### REM Query API

```bash
# Structured query
curl -X POST http://localhost:8000/query/ \
  -H "Content-Type: application/json" \
  -d '{"mode": "LOOKUP", "key": "demo-project-planning"}'

curl -X POST http://localhost:8000/query/ \
  -H "Content-Type: application/json" \
  -d '{"mode": "FUZZY", "query": "knowledge graph", "threshold": 0.4, "limit": 5}'

# Raw REM dialect query
curl -X POST http://localhost:8000/query/raw \
  -H "Content-Type: application/json" \
  -d '{"query": "SEARCH \"database migration\" FROM resources LIMIT 10"}'
```

### Content Upload

```bash
# Upload a file for extraction, chunking, and embedding
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: 550e8400-e29b-41d4-a716-446655440000" \
  -F "file=@paper.pdf" \
  -F "category=research" \
  -F "tags=ml,papers"
```

### MCP Server

The MCP server is mounted at `/mcp` and exposes `search`, `action`, and `ask_agent` tools via the Streamable HTTP transport. Connect any MCP client (Claude Desktop, Cursor, etc.) to `http://localhost:8000/mcp`.

## Services

p8 ships with a set of integrated services that handle encryption, authentication, storage, content processing, and billing.

### Encryption

Envelope encryption with pluggable KMS backends (`P8_KMS_PROVIDER`):

- **`local`** (default) — AES-256-GCM with a file-based master key. Good for dev and single-node.
- **`vault`** — HashiCorp Vault Transit engine. Key material never leaves Vault.

Three encryption modes per tenant: `platform` (transparent server-side), `client` (server encrypts, client decrypts), `sealed` (RSA-OAEP hybrid — server never holds the private key). Fields are encrypted at rest automatically via the Repository layer. Deterministic encryption is used for searchable fields like email.

### Auth

Full multi-provider OAuth + JWT + magic link authentication:

- **OAuth**: Google, Apple Sign-In (web + mobile deep-link flows)
- **JWT**: HS256 access tokens (1h) + refresh tokens (30d) with single-use rotation
- **Magic links**: Email-based passwordless auth via console, SMTP, or Resend
- **Tenant isolation**: Every user belongs to a tenant; all queries are scoped by `tenant_id`
- **Google Drive**: OAuth grant for mobile clients to connect cloud storage

### S3 / File Storage

Unified `FileService` abstraction over local filesystem and S3:

- Reads/writes by path or `s3://bucket/key` URI
- Supports custom S3 endpoints (`P8_S3_ENDPOINT_URL`) for Hetzner Object Storage or MinIO
- Date-partitioned keys: `{user_id}/{YYYY}/{MM}/{DD}/{filename}`

### Content Pipeline

`ContentService` handles the full ingestion pipeline:

1. **Upload** to S3 (if configured)
2. **Extract** text — documents via Kreuzberg, audio via Whisper transcription, images (planned)
3. **Chunk** with configurable size/overlap (default 1500 chars, 200 overlap)
4. **Persist** as `File` + `Resource` entities with `graph_edges` linking chunks to source
5. **Embed** automatically via PostgreSQL triggers → embedding queue → worker

### Embeddings

Pluggable embedding providers (`P8_EMBEDDING_MODEL`):

| Provider | Model | Dimensions | Use case |
|----------|-------|------------|----------|
| `openai:text-embedding-3-small` | text-embedding-3-small | 1536 | Production (default) |
| `fastembed:BAAI/bge-small-en-v1.5` | bge-small-en-v1.5 | 384 | Local, no API key |
| `local` | SHA-512 hash | configurable | Tests only |

Queue-based processing: PostgreSQL triggers enqueue embedding jobs → `EmbeddingWorker` polls and batch-processes with content-hash deduplication.

### Usage & Billing

Plan-based quota enforcement with Stripe integration:

- **Metered resources**: chat tokens, storage bytes, dreaming minutes, cloud folders
- **Plans**: `free`, `pro`, `team`, `enterprise` with configurable limits
- **Add-ons**: One-time Stripe Checkout purchases that credit extra quota
- **Stripe webhooks**: Subscription lifecycle, plan changes, add-on fulfillment

## Configuration

All settings via environment variables with `P8_` prefix, or `.env` file. See `p8/settings.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `P8_DATABASE_URL` | `postgresql://p8:p8_dev@localhost:5488/p8` | Postgres connection |
| `P8_EMBEDDING_MODEL` | `openai:text-embedding-3-small` | Embeddings (1536d) |
| `P8_KMS_PROVIDER` | `local` | `local` or `vault` |
| `P8_OPENAI_API_KEY` | — | Required for embeddings and LLM |
| `P8_DEFAULT_MODEL` | `openai:gpt-4o-mini` | Default LLM for agents |
| `P8_API_KEY` | — | API key for endpoint protection |
| `P8_S3_BUCKET` | — | S3 bucket for file storage |
| `P8_S3_ENDPOINT_URL` | — | Custom S3 endpoint (Hetzner, MinIO) |

## Project Structure

```
p8k8/
├── p8/                    # Python package
│   ├── api/               # FastAPI + MCP server + CLI
│   │   ├── main.py        # App factory, lifespan, router mounts
│   │   ├── mcp_server.py  # FastMCP: search, action, ask_agent
│   │   ├── controllers/   # ChatController (shared API + CLI logic)
│   │   ├── routers/       # chat, query, content, schemas, moments, auth, admin, payments
│   │   ├── tools/         # MCP tool implementations
│   │   └── cli/           # Typer CLI (serve, migrate, query, upsert, chat, schema, moments)
│   ├── services/          # Business logic
│   │   ├── database/      # asyncpg pool, REM query engine
│   │   ├── repository.py  # Generic typed CRUD with encryption
│   │   ├── encryption.py  # Envelope encryption, per-tenant DEKs
│   │   ├── auth.py        # OAuth, JWT, magic links, tenant management
│   │   ├── content.py     # Ingestion pipeline (extract → chunk → persist)
│   │   ├── files.py       # Local + S3 file abstraction
│   │   ├── embeddings.py  # Pluggable providers + queue worker
│   │   ├── memory.py      # Context loading, moment compaction
│   │   ├── queue.py       # PostgreSQL task queue (KEDA-scaled workers)
│   │   ├── usage.py       # Plan-based quota enforcement
│   │   ├── stripe.py      # Stripe billing integration
│   │   └── bootstrap.py   # Service wiring + lifecycle
│   ├── ontology/          # Pydantic models → JSON Schema → Postgres
│   ├── agentic/           # Agent adapter, streaming, delegation, routing
│   └── settings.py        # P8_ env prefix
├── sql/                   # PostgreSQL init scripts
├── docker/                # Dockerfile.pg18 (local dev)
├── tests/                 # Test suite
├── manifests/             # K8s manifests
│   ├── platform/          # cert-manager, cloudnative-pg
│   └── application/
│       └── p8-stack/      # API + Postgres + Workers
│           └── overlays/
│               ├── local/
│               └── hetzner/
├── Dockerfile             # Production container image
├── docker-compose.yml     # Local dev (postgres + KMS)
└── pyproject.toml         # name=p8
```
