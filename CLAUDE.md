# CLAUDE.md — p8

> Deployable agentic memory API for Hetzner K8s. PostgreSQL 18, pgvector, pydantic-ai, FastAPI, FastMCP, AG-UI.
> Forked from remslim, packaged as `p8` for the `p8-w-1` Hetzner cluster.

If you follow the instructions to ingest the ontology and the MCP server is enabled you can search it for details on the project.

## Overview

Minimal agentic framework where **ontology is everything**. Every entity — models, agents, evaluators, tools — is a row in the `schemas` table.

- **p8/ontology/** — Pydantic models -> JSON Schema -> Postgres tables -> agents
- **p8/services/** — Database (REM queries), embeddings, repository, encryption, content, queue, usage, web_search, files, graph, notifications, stripe, providers
- **p8/agentic/** — pydantic-ai agent adapter, agent_schema, streaming, delegation, routing, core_agents, otel
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
├── services/       # database/, repository, embeddings, encryption, kms, memory, content, queue, usage, files, graph, notifications, stripe, providers
├── agentic/        # adapter, agent_schema, streaming, delegate, routing, core_agents, types, otel
├── api/
│   ├── main.py         # FastAPI app factory + lifespan
│   ├── mcp_server.py   # FastMCP: search, action, ask_agent, get_moments, web_search, update_user_metadata, remind_me + user://profile resource
│   ├── controllers/    # ChatController (shared API+CLI logic)
│   ├── routers/        # chat, query, schemas, moments, admin, auth, content, embeddings, resources, notifications, share, payments
│   ├── tools/          # MCP tool implementations (search, action, ask_agent, get_moments, web_search, update_user_metadata, remind_me, save_moments)
│   └── cli/            # Typer CLI (serve, migrate, query, upsert, schema, chat, moments, dream, admin, db, encryption, mcp, verify-links)
├── workers/        # TieredWorker processor, task handlers (dreaming, file_processing, news, reading, scheduled)
├── utils/          # Parsing, token estimation, ID generation, data, links
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

Four idempotent SQL scripts, run in order by `p8 migrate`:

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
p8 mcp                                          # Start FastMCP stdio server
p8 verify-links <PATH> [--check-db]             # Check markdown links
p8 encryption status                             # KMS/encryption status
p8 encryption configure <TENANT> <MODE>          # Configure tenant encryption
p8 encryption test <TENANT> <MODE>               # Test encryption round-trip
p8 encryption test-isolation                     # Test cross-tenant isolation
p8 db diff [--remote-url URL]                    # Diff local vs remote schema
p8 db apply <SQL_FILE> [--target-url URL] [--dry-run]

# Admin (defaults to remote Hetzner via port-forward; use --local for docker-compose)
p8 admin health [--email PARTIAL] [--user UUID]
p8 admin queue [--status pending|failed] [--detail] [--type TYPE]
p8 admin quota [--email EMAIL] [--user UUID] [--reset] [--resource chat_tokens]
p8 admin enqueue <TASK_TYPE> --user UUID [--delay MINUTES]
p8 admin heal-jobs                               # Fix stale reminder cron jobs
```

## Port Conventions

See [docs/port-conventions.md](docs/port-conventions.md) for the full table.

| Port | Use |
|------|-----|
| 5489 | Local dev PostgreSQL (`p8k8-db` container) |
| 5490 | Test PostgreSQL (disposable) |
| 5491 | kubectl port-forward to Hetzner K8s |
| 5488 | Legacy `remslim` stack — do not reuse |
| 8000 | API server (`p8 serve`) |
| 8200 | OpenBao KMS |

**Important:** kubectl port-forwards on `localhost` shadow Docker's `0.0.0.0` binding on the same port. Kill port-forwards (`lsof -i :5489`) before local dev.

## Settings

`p8/settings.py` — pydantic-settings with `P8_` env prefix. Key settings:

- `database_url` — Postgres connection (default: `postgresql://p8:p8_dev@localhost:5489/p8`)
- `embedding_model` — `openai:text-embedding-3-small` (default, 1536d), `local` (tests only)
- `kms_provider` — `local` | `vault` | `aws`
- `context_token_budget` / `always_include_last_messages` — memory compaction config

## Architecture: AWS vs Hetzner Recipe

Two deployment recipes exist for the p8 stack:

- **AWS recipe** (`reminiscent/` repo): SQS queue + KEDA SQS trigger + ExternalSecrets from AWS Parameter Store + S3 native events. Full CDK setup.
- **Hetzner recipe** (this repo): PostgreSQL queue (`task_queue`) + KEDA postgresql trigger + ESO-managed secrets from OpenBao KV v2. Lighter weight, no NATS.

**Hetzner stack**: API (chat, file upload, MCP server) + CloudNativePG PostgreSQL + KEDA-scaled tiered workers (file_processing, dreaming, news, reading, scheduled).

## Default Test User

**Sage Whitfield** — user_id `7d31eddf-7ff7-542a-982f-7522e7a3ec67` (row id `e76db623-9067-5688-9d24-e05bff36b694`, email `user@example.com`)

Restoration ecologist from the Pacific Northwest. Interests: forest ecology, birdwatching, mushroom foraging, trail running, woodworking, field recording, wildlife photography, permaculture. Has a border collie named Cedar. Subscribed to Audubon News, Treehugger, iNaturalist, and US Forest Service feeds. Seeded in `sql/02_install.sql`.

## User ID from Email

User IDs are deterministic UUID5 hashes of the email: `deterministic_id("users", email)` from `p8/ontology/base.py`. To find a user without relying on email decryption:

```python
from p8.ontology.base import deterministic_id
user_id = deterministic_id("users", "someone@example.com")
# Then: p8 admin quota --user <user_id> --reset
# Or simply: p8 admin quota --email someone@example.com --reset
```

This avoids encryption/Vault issues with `--email` lookups in admin commands.

## Git Commits

- NEVER add `Co-Authored-By` or any AI attribution lines to commit messages
- Keep commit messages concise — summary line + optional body

## Secret Management (Hetzner)

Secrets are managed by **ESO (External Secrets Operator)** pulling from **OpenBao KV v2**:

```
.env → seed-openbao.sh → OpenBao KV v2 → ESO (1h refresh) → K8s Secrets → Pods
```

| K8s Secret | OpenBao Path | Contents |
|------------|-------------|----------|
| `p8-app-secrets` | `secret/p8/app-secrets` | API keys, OAuth, Stripe, Slack, etc. |
| `p8-database-credentials` | `secret/p8/database-credentials` | username + password |
| `p8-keda-pg-connection` | `secret/p8/keda-pg-connection` | connection string |

Bootstrap secrets (manual, one-time, created by `init-openbao.sh`):
- `openbao-unseal-keys` — 3 unseal keys + root token
- `openbao-eso-token` — root token for ESO auth

### Adding a secret to an existing K8s secret

```bash
# Get root token
ROOT_TOKEN=$(kubectl --context=p8-w-1 -n p8 get secret openbao-unseal-keys \
  -o jsonpath='{.data.root_token}' | base64 -d)

# Use `kv patch` to add/update without overwriting other keys
kubectl --context=p8-w-1 -n p8 exec openbao-0 -c openbao -- env \
  BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$ROOT_TOKEN" \
  bao kv patch secret/p8/app-secrets NEW_KEY=value

# Force ESO sync (default is 1h)
kubectl --context=p8-w-1 -n p8 annotate externalsecrets p8-app-secrets \
  force-sync=$(date +%s) --overwrite

# Restart pods
kubectl --context=p8-w-1 -n p8 rollout restart deploy/p8-api
```

### Adding a new K8s secret (new ExternalSecret)

1. Write to a new KV path: `bao kv put secret/p8/my-secret key=value`
2. Create `manifests/platform/external-secrets/external-secret-my-secret.yaml`
3. Add to `manifests/platform/external-secrets/kustomization.yaml`
4. Apply: `kubectl --context=p8-w-1 apply -k manifests/platform/external-secrets/`

### Bulk seed from .env

```bash
./manifests/scripts/seed-openbao.sh --context=p8-w-1
```

## Deployment (Hetzner p8-w-1)

```bash
# Build and push container image
docker buildx build --platform linux/amd64 \
  -t percolationlabs/p8k8:latest --push .

# Deploy the full stack (includes ESO ExternalSecrets, OpenBao, etc.)
kubectl --context=p8-w-1 apply -k manifests/application/p8-stack/overlays/hetzner/

# Run migrations
p8 migrate

# Seed secrets from .env into OpenBao (after init)
./manifests/scripts/seed-openbao.sh --context=p8-w-1

# Local dev
docker compose up -d --build
p8 migrate
uv run p8 serve
```
