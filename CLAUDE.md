# CLAUDE.md — p8

> Deployable agentic memory API for Hetzner K8s. PostgreSQL 18, pgvector, pydantic-ai, FastAPI, FastMCP, AG-UI.
> Forked from remslim, packaged as `p8` for the `p8-w-1` Hetzner cluster.

If you follow the instructions to ingest the ontology and the MCP server is enabled you can search it for details on the project.

## Overview

Minimal agentic framework where **ontology is everything**. Every entity — models, agents, evaluators, tools — is a row in the `schemas` table.

- **p8/ontology/** — Pydantic models -> JSON Schema -> Postgres tables -> agents
- **p8/services/** — Database (REM queries), embeddings, repository, encryption, content, queue, usage, web_search
- **p8/agentic/** — pydantic-ai agent adapter, streaming, delegation, routing
- **p8/api/** — FastAPI + FastMCP server, AG-UI chat, CLI (Typer)

## Key Design Rules

1. **Schemas ARE agents** — `kind='agent'` row: `content` = system prompt, `json_schema` = config
2. **Remote tools only** — Tools point to MCP/OpenAPI servers via `ToolReference`, never inline code
3. **Embed by default** — Content fields get vectors automatically; set `embedding_field = None` to opt out
4. **UNLOGGED for speed** — `kv_store` and `embedding_queue` use UNLOGGED tables (rebuilt on crash)
5. **Lazy over eager** — Prefer lazy agent routing (persist last agent) over per-turn classification
6. **Redact before embed** — PII pipeline runs before embedding generation
7. **Slim is the goal** — Minimal files, minimal abstraction, maximum capability

## Architecture

```
p8/
├── ontology/       # CoreModel (base.py), built-in types (types.py), verify
├── services/       # database/, repository, embeddings, encryption, kms, memory, content
├── agentic/        # adapter, streaming, delegate, routing, types
├── api/
│   ├── main.py         # FastAPI app factory + lifespan
│   ├── mcp_server.py   # FastMCP: search, action, ask_agent
│   ├── controllers/    # ChatController (shared API+CLI logic)
│   ├── routers/        # chat, query, schemas, moments, admin, auth, content, embeddings
│   ├── tools/          # MCP tool implementations
│   └── cli/            # Typer CLI (serve, migrate, query, upsert, schema, chat, moments)
├── settings.py
sql/
├── 01_install_entities.sql  # Entity tables + embeddings tables
├── 02_install.sql           # KV store, REM functions, triggers, indexes
├── 03_qms.sql               # Queue management system
└── 04_payments.sql          # Stripe payment tables
manifests/                # K8s manifests for Hetzner deployment
docker/                   # Dockerfile.pg18 for local dev postgres
```

## REM Query Modes

All in `p8/services/database/`. See `sql/02_install.sql` for function signatures.

| Mode | Function | Use Case |
|------|----------|----------|
| LOOKUP | `rem_lookup(key)` | O(1) entity by name via kv_store |
| SEARCH | `rem_search(embedding, table)` | Semantic similarity via pgvector |
| FUZZY | `rem_fuzzy(text)` | Trigram matching via pg_trgm |
| TRAVERSE | `rem_traverse(key, depth)` | Graph walk via graph_edges JSONB |
| TIMELINE | `rem_session_timeline(session_id)` | Interleaved messages + moments for a session |
| SQL | Direct queries | Full Postgres SQL |

## Database

Two idempotent SQL scripts, run in order by `p8 migrate`:

1. `sql/01_install_entities.sql` — extensions, entity tables, embeddings companion tables
2. `sql/02_install.sql` — kv_store, embedding_queue, helper functions, REM functions, triggers, indexes, pg_cron jobs
3. `sql/03_qms.sql` — queue management system (task_queue, claim/fail/complete, pg_cron)
4. `sql/04_payments.sql` — Stripe payment tables

## CLI

```bash
p8 serve [--port 8000] [--reload]
p8 migrate
p8 query 'LOOKUP "demo-project-planning"'
p8 upsert schemas data/agents.yaml
p8 upsert docs/architecture.md
p8 schema list [--kind agent]
p8 schema verify
p8 chat [SESSION_ID] [--agent query-agent]
p8 dream <USER_ID> [--lookback 7] [--allow-empty]
p8 moments [--type session_chunk]
p8 moments timeline SESSION_ID
```

## Settings

`p8/settings.py` — pydantic-settings with `P8_` env prefix. Key settings:

- `database_url` — Postgres connection (default: `postgresql://p8:p8_dev@localhost:5488/p8`)
- `embedding_model` — `openai:text-embedding-3-small` (default, 1536d), `local` (tests only)
- `kms_provider` — `local` | `vault` | `aws`
- `context_token_budget` / `always_include_last_messages` — memory compaction config

## Architecture: AWS vs Hetzner Recipe

Two deployment recipes exist for the p8 stack:

- **AWS recipe** (`reminiscent/` repo): SQS queue + KEDA SQS trigger + ExternalSecrets from AWS Parameter Store + S3 native events. Full CDK setup.
- **Hetzner recipe** (this repo): PostgreSQL queue (`files.processing_status`) + KEDA postgresql trigger + plain K8s Secrets from `.env`. Lighter weight, no NATS.

**Hetzner stack**: API (chat, file upload, MCP server) + CloudNativePG PostgreSQL + KEDA-scaled file worker (2GB RAM) + optional dreaming CronJob.

## Default Test User

**Sage Whitfield** — user_id `7d31eddf-7ff7-542a-982f-7522e7a3ec67` (row id `e76db623-9067-5688-9d24-e05bff36b694`, email `user@example.com`)

Restoration ecologist from the Pacific Northwest. Interests: forest ecology, birdwatching, mushroom foraging, trail running, woodworking, field recording, wildlife photography, permaculture. Has a border collie named Cedar. Subscribed to Audubon News, Treehugger, iNaturalist, and US Forest Service feeds. Seeded in `sql/02_install.sql`.

## Git Commits

- NEVER add `Co-Authored-By` or any AI attribution lines to commit messages
- Keep commit messages concise — summary line + optional body

## Deployment (Hetzner p8-w-1)

```bash
# Build and push container image
docker buildx build --platform linux/amd64 \
  -t percolationlabs/p8k8:latest --push .

# Create namespace
kubectl --context=p8-w-1 create namespace p8 --dry-run=client -o yaml | kubectl apply -f -

# Create secrets from .env (or edit overlays/hetzner/secrets.yaml)
kubectl --context=p8-w-1 -n p8 create secret generic p8-database-credentials \
  --from-literal=username=p8user --from-literal=password=REAL_PASSWORD \
  --dry-run=client -o yaml | kubectl apply -f -

# Deploy the full stack
kubectl --context=p8-w-1 apply -k manifests/application/p8-stack/overlays/hetzner/

# Run migrations (blank DB — no SQL baked into the PG image)
p8 migrate

# Local dev
docker compose up -d --build
p8 migrate
uv run p8 serve
```
