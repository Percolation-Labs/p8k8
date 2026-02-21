# p8

Cloud-native agentic memory framework. Postgres does the heavy lifting — pgvector for embeddings, pg_trgm for fuzzy search, JSONB graph edges for traversal, pg_cron for scheduled tasks. The Python layer is intentionally thin: FastAPI for HTTP, FastMCP for tool serving, pydantic-ai for agent orchestration. Every entity — models, agents, tools, ontologies — is a row in the `schemas` table.

## What is REM?

REM (Resource-Entity-Moment) is the query engine. It sits on top of a unified knowledge base stored in Postgres and exposes five query modes, all implemented as Postgres functions:

| Mode | Syntax | What it does |
|------|--------|--------------|
| LOOKUP | `LOOKUP "key"` | O(1) key-value fetch via `kv_store` |
| SEARCH | `SEARCH "text" FROM table` | Semantic similarity via pgvector |
| FUZZY | `FUZZY "text"` | Trigram matching via pg_trgm |
| TRAVERSE | `TRAVERSE "key" DEPTH n` | Graph walk via JSONB edges |
| SQL | `SQL SELECT ...` | Direct Postgres (read-only) |

You can run REM queries from the CLI (`p8 query`), the API (`POST /query/raw`), or through MCP tools (`search`).

## Architecture

Two deployment recipes exist:

- **Hetzner recipe** (this repo) — CloudNativePG Postgres, KEDA-scaled file workers, OpenBao KMS, OTEL collector. Plain K8s secrets from `.env`. Lighter weight, no NATS.
- **AWS recipe** (`reminiscent/` repo) — SQS + KEDA SQS trigger, ExternalSecrets from AWS Parameter Store, S3 native events. Full CDK setup.

```
p8k8/
├── p8/
│   ├── ontology/       # Pydantic models -> JSON Schema -> Postgres
│   ├── services/       # Database (REM), embeddings, encryption, content, memory
│   ├── agentic/        # Agent adapter, streaming, delegation, routing
│   └── api/
│       ├── main.py     # FastAPI app factory
│       ├── mcp_server.py
│       ├── routers/    # HTTP endpoints
│       ├── controllers/# Shared API + CLI logic
│       └── cli/        # Typer CLI commands
├── sql/                # Postgres init scripts (idempotent)
├── manifests/          # K8s manifests (Hetzner + local overlays)
├── docker/             # Dockerfile.pg18 (local dev Postgres)
└── docker-compose.yml  # Local dev (Postgres + OpenBao KMS)
```

## Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Configure
cp .env.example .env
# Edit .env — set P8_OPENAI_API_KEY=sk-...

# 3. Start Postgres + OpenBao KMS (auto-runs all sql/ init scripts)
docker compose up -d --build

# 4. Start the API server
uv run p8 serve --port 8000

# 5. Health check
curl http://localhost:8000/health
# → {"status":"ok"}

# 6. Streaming chat (AG-UI protocol)
curl -N -X POST http://localhost:8000/chat/test-session-1 \
  -H "Content-Type: application/json" \
  -H "x-agent-schema-name: general" \
  -H "x-user-id: 66fd910d-beba-56d5-a50a-ffb147ce0569" \
  -d '{
    "threadId": "test-session-1",
    "runId": "run-1",
    "messages": [{"id": "msg-1", "role": "user", "content": "hello"}],
    "tools": [], "context": [], "forwardedProps": {}, "state": null
  }'
```

Docker compose starts Postgres 18 (with pgvector, pg_cron) on port `5488` and OpenBao KMS on port `8200`. Use `p8 migrate` to re-apply schema changes to an existing database.

## CLI Walkthrough

The `p8` CLI is the primary interface. Here's a typical workflow:

```bash
# Upload documents into the knowledge base
p8 upsert docs/ontology/

# Chat with an agent (it can search what you just uploaded)
p8 chat

# See what the system remembered from the conversation
p8 moments

# Query the knowledge base directly
p8 query 'SEARCH "deployment architecture" FROM ontologies LIMIT 5'
p8 query 'LOOKUP "rem-search"'
```

See [CLI reference](p8/api/cli/README.md) for the full command list.

## Custom Ontology

Markdown files in `docs/` become ontology rows — small, focused knowledge pages that agents can look up by name or find by meaning. Each file maps to one entity: filename = entity key, content = embedded, markdown links = graph edges.

```markdown
# rem-search

Semantic similarity search over any entity table using pgvector.

Related: [REM Overview](rem-overview), [Embedding Service](embedding-service).
```

Ingest and query:

```bash
p8 upsert docs/ontology/                    # ingest all pages
p8 verify-links docs/ontology/              # check links resolve
p8 query 'SEARCH "how to find similar content" FROM ontologies LIMIT 5'
p8 query 'TRAVERSE "rem-overview" DEPTH 2'  # follow graph edges
```

See [docs/ontology/README.md](docs/ontology/README.md) for link conventions, categories, and file size rules.

## Agents

Agents come from three sources, checked in order:

1. **Database** — `schemas` table rows with `kind='agent'` (highest priority)
2. **Built-in** — Python classes in `p8/agentic/core_agents.py` (general, dreaming-agent, sample-agent)
3. **YAML** — Files in the `P8_SCHEMA_DIR` folder (hot-reloaded on lookup)

An agent is just a schema row where `content` is the system prompt and `json_schema` holds config (tools, model, limits). Example YAML:

```yaml
- name: my-agent
  kind: agent
  description: A domain-specific assistant
  content: |
    You are an expert in distributed systems.
    Search the knowledge base before answering.
  json_schema:
    tools:
      - name: search
      - name: ask_agent
    model: openai:gpt-4.1
    temperature: 0.2
```

Load it:

```bash
p8 upsert schemas agents.yaml
```

## Testing

```bash
# Configure git hooks (one-time setup)
git config core.hooksPath .githooks

# Unit tests (fast, no external deps)
uv run pytest tests/unit/

# Integration tests (requires Postgres)
docker compose -p p8-test -f docker-compose.test.yml up -d --wait
uv run pytest tests/integration/
docker compose -p p8-test -f docker-compose.test.yml down

# All tests
uv run pytest
```

Pre-commit runs unit tests. Pre-push starts an ephemeral Postgres on port 5499 and runs integration tests.

## Deployment

Hetzner K8s deployment:

```bash
docker buildx build --platform linux/amd64 \
  -t percolationlabs/p8k8:latest --push .

kubectl --context=p8-w-1 apply -k manifests/application/p8-stack/overlays/hetzner/
```

See [CLAUDE.md](CLAUDE.md) for the full recipe (configmap creation, secrets, namespace setup).

## Configuration

All settings via `P8_` environment variables or `.env` file. See [.env.example](.env.example) for the full list.

| Variable | Default | Description |
|----------|---------|-------------|
| `P8_OPENAI_API_KEY` | — | **Required.** Powers LLM and embeddings |
| `P8_DATABASE_URL` | `postgresql://p8:p8_dev@localhost:5488/p8` | Postgres connection |
| `P8_KMS_PROVIDER` | `local` | Encryption backend (`local` \| `vault` \| `aws`) |
| `P8_DEFAULT_MODEL` | `openai:gpt-4.1` | Default LLM for agents |
| `P8_EMBEDDING_MODEL` | `openai:text-embedding-3-small` | Embedding provider |

## Links

- [API reference](p8/api/README.md) — endpoints, headers, curl examples, AG-UI streaming, MCP setup
- [CLI reference](p8/api/cli/README.md) — all commands, flags, service mapping
- [CLAUDE.md](CLAUDE.md) — architecture deep dive, deployment recipe, design rules
