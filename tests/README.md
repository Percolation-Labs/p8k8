# Tests

All tests run with `P8_EMBEDDING_MODEL=local` (no API keys needed). Most require a live Postgres instance (`docker compose up -d`).

```bash
# Run all tests
uv run pytest

# Run a specific group
uv run pytest tests/database/
uv run pytest tests/agents/
uv run pytest tests/memory/

```

## Structure

```
tests/
├── conftest.py              # Shared fixtures (db, encryption, settings)
├── data/                    # Seed data and test fixtures
│   ├── conversations.json
│   ├── seed-session.json
│   ├── seed-messages.json
│   ├── seed-feed.json
│   └── uploads/             # Sample files for ingestion tests
├── database/                # Database & storage layer
│   ├── test_database.py     # Bootstrap, triggers, rem_* functions, session clone/search
│   ├── test_repository.py   # Repository.upsert() across all 13 entity models
│   ├── test_kv_store.py     # KV triggers, normalize_key(), indexes, rebuild
│   └── test_query_engine.py # RemQueryParser, dispatch, SQL safety, live roundtrips
├── memory/                  # Memory & moments
│   ├── test_memory.py       # MemoryService — persist/load, compaction, encryption
│   ├── test_moments.py      # Moment building, chaining, timeline, today summary
│   ├── test_moments_feed.py # rem_moments_feed — daily summaries, pagination, scoping
│   └── test_memory_pipeline.py  # End-to-end: compaction → breadcrumbs → replay
├── agents/                  # Agents & chat
│   ├── test_chat.py         # Chat endpoint, AgentAdapter, config, routing, streaming
│   ├── test_agent_tools.py  # Agent construction, MCP tools, delegation, event sink
│   ├── test_tools.py        # MCP tool implementations (search, action, user_profile)
│   └── test_remind_me.py    # remind_me tool — pg_cron scheduling, one-time/recurring, validation
├── security/                # Encryption & auth
│   ├── test_encryption.py   # Encryption modes, tenant isolation, deterministic, own-key
│   ├── test_vault.py        # VaultTransitKMS (requires OpenBao on localhost:8200)
│   └── test_auth_flows.py   # JWT, refresh rotation, magic links, OAuth callbacks
├── content/                 # Content & embeddings
│   ├── test_content.py      # ContentService — ingest, chunk, extract (all mocked)
│   └── test_embeddings.py   # Embedding pipeline, providers, queue worker, API
├── api/                     # API & CLI
│   ├── test_api.py          # FastAPI endpoints — health, schemas, queries, upsert chain
│   └── test_cli.py          # Typer commands — query, upsert, chat (mocked + live)
└── ontology/                # Schema verification
    └── test_verify.py       # verify_model(), verify_all(), schema register, CLI

├── dreaming/                # Dreaming agent
│   ├── test_dreaming.py     # Dreaming handler — context building, agent invocation, moment persistence
│   ├── test_dreaming_e2e.py # End-to-end dreaming with FunctionModel — phases 1-3
│   ├── test_save_moments.py # save_moments tool — dream moments, graph edges, back-edges
│   └── test_merge_graph_edges.py # Graph edge merging logic
├── .sims/                   # Simulations — demos, diagnostics, scripted workflows
│   └── ...
```

## Groups

### database/ — Database & Storage

Core data layer — entity tables, triggers, KV store, and the Repository upsert pipeline.

| Test | What it covers |
|------|----------------|
| `test_database` | Bootstrap, extensions, entity tables, all `rem_*` functions, triggers (KV sync, soft-delete, embedding queue, timemachine), session cloning and search |
| `test_repository` | `Repository.upsert()` across all 13 entity models — encryption, multi-tenancy, bulk ops, JSONB fields, graph edges, FK constraints, read-after-write |
| `test_kv_store` | KV insert/update/delete triggers, `normalize_key()`, trigram + GIN + HNSW indexes, `rebuild_kv_store()`, REM functions via KV |
| `test_query_engine` | `RemQueryParser` for all 5 modes, `RemQueryEngine` dispatch (mocked + live), SQL safety guards (DROP/TRUNCATE/ALTER blocked), `build_rem_prompt()`, implicit SQL fallback |

### memory/ — Memory & Moments

Conversation memory loading, compaction, moment building, and the temporal feed.

| Test | What it covers |
|------|----------------|
| `test_memory` | `MemoryService` — persist/load messages, token-budget compaction, moment injection, encrypted messages, auto token counting |
| `test_moments` | Moment threshold triggering, moment chaining, context injection, today summary, session timeline interleaving, content-upload moments |
| `test_moments_feed` | `rem_moments_feed` — paginated feed with virtual daily summaries, cursor pagination, user scoping, deterministic session IDs |
| `test_memory_pipeline` | End-to-end: compaction → resolvable KV breadcrumbs, multi-turn sessions, moment chaining across batches, full pipeline replay from seed data, upload + chat moments |

### agents/ — Agents & Chat

Agent construction, tool resolution, chat endpoint, and streaming.

| Test | What it covers |
|------|----------------|
| `test_chat` | Chat HTTP endpoint (headers, sessions, SSE streaming, persistence), `AgentAdapter` internals (schema loading, structured output via `to_output_schema()`, thinking structure via `to_prompt()`, routing state, message history conversion, context injection, `FunctionModel` capture) |
| `test_agent_tools` | AgentSchema `from_model_class` / `from_yaml_file` / `from_schema_row`, tool notes in system prompt, thinking aides in prompt, DB + YAML round-trips, TTL caching, MCP tool resolution (`FastMCPToolset` vs delegates), `search`/`action`/`ask_agent` tool invocation, delegation with event sink, legacy schema backward compat |
| `test_tools` | MCP tool implementations — `search` (LOOKUP, FUZZY, SQL), `action` (observation), `user_profile` resource |
| `test_remind_me` | `remind_me` tool — one-time ISO datetime scheduling, recurring cron expressions, invalid cron validation, missing user_id, pg_cron job creation + payload verification, auto-unschedule for one-time jobs |

### security/ — Encryption & Auth

Security layer — envelope encryption, KMS backends, OAuth, JWT, magic links.

| Test | What it covers |
|------|----------------|
| `test_encryption` | Encryption modes via Repository — platform (transparent), client (ciphertext returned), deterministic (email search), tenant isolation, disabled mode, system key fallback, own-key |
| `test_vault` | `VaultTransitKMS` integration — platform/client modes, system key fallback, tenant isolation. **Requires OpenBao/Vault on localhost:8200** |
| `test_auth_flows` | `AuthService` — JWT create/verify/expiry, refresh rotation, revocation, magic link full flow (create → verify → single-use), Google/Apple OAuth callbacks |

### content/ — Content & Embeddings

Ingestion pipeline and embedding generation.

| Test | What it covers |
|------|----------------|
| `test_content` | `ContentService` — `load_structured` (JSON/YAML), `ingest()` for PDF/audio/image, chunking, S3 upload, graph edges, `upsert_markdown()`, `upsert_structured()`. All mocked (no DB needed) |
| `test_embeddings` | Embedding pipeline — upsert triggers → KV + embeddings via `EmbeddingWorker`, content-hash caching, `LocalEmbeddingProvider` unit tests, `/embeddings/process` and `/embeddings/generate` API endpoints |

### api/ — API & CLI

HTTP endpoints and Typer CLI commands.

| Test | What it covers |
|------|----------------|
| `test_api` | FastAPI endpoints — health, schema CRUD, LOOKUP/FUZZY queries, KV rebuild, queue status, full upsert chain (deterministic ID → KV → embedding queue → verify) |
| `test_cli` | Typer commands — `query` (one-shot modes, table format, errors), `upsert` (JSON/YAML/Markdown/directories), `chat` (session creation, agent selection, delegation). Mostly mocked, with live DB integration for query roundtrips |

### dreaming/ — Dreaming Agent

Background reflective agent that generates dream moments from recent activity.

| Test | What it covers |
|------|----------------|
| `test_dreaming` | Dreaming handler — context building from recent sessions, agent invocation with mocked model, moment persistence |
| `test_dreaming_e2e` | End-to-end dreaming with `FunctionModel` — all 3 phases (reflect, search, save), graph edge creation |
| `test_save_moments` | `save_moments` MCP tool — dream moment creation, affinity→graph_edges, bidirectional back-edges on targets, graceful missing target |
| `test_merge_graph_edges` | Graph edge merging — dedup, weight update, empty lists, field preservation, default relation |

### ontology/ — Schema Verification

| Test | What it covers |
|------|----------------|
| `test_verify` | `verify_model()` checks (missing table/column, extra columns, embedding tables, triggers, schema metadata), `verify_all()` iteration, CLI `schema verify` / `schema register`, live integration (clean DB pass, idempotent register) |

## .sims/ — Simulations

The `.sims/` directory holds scripted scenarios that are test-like but serve a different purpose — demos, diagnostics, and end-to-end workflow replays. They exercise the full stack (real database, real agents, real MCP tools, real memory compaction) but are not part of the regular `pytest` suite.

Use sims to: demonstrate a feature to stakeholders, diagnose a production issue by replaying a workflow, or validate a cross-cutting scenario that spans multiple services (e.g. create a user → upload documents → chat with an agent → verify moments → resume session).

```bash
# Run a specific sim directly
uv run python tests/.sims/onboarding_demo.py
```
