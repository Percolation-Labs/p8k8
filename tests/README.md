# Tests

All tests run with `P8_EMBEDDING_MODEL=local` (no API keys needed). Integration tests require a live Postgres instance (`docker compose up -d`). Unit tests are fully mocked.

```bash
# Run all tests
uv run pytest

# Run unit tests only (no DB needed)
uv run pytest tests/unit/

# Run integration tests (needs Postgres)
uv run pytest tests/integration/

# Run a specific group
uv run pytest tests/integration/database/
uv run pytest tests/integration/agents/
uv run pytest tests/integration/security/
```

## Structure

```
tests/
├── conftest.py                     # Shared fixtures (db, encryption, settings)
├── data/                           # Seed data and test fixtures
│   ├── conversations.json
│   ├── seed-session.json
│   ├── seed-messages.json
│   ├── seed-feed.json
│   ├── fixtures/                   # Persona seed data (Jamie Rivera, etc.)
│   └── uploads/                    # Sample files for ingestion tests
├── unit/                           # Pure unit tests — no DB, all mocked
│   ├── helpers.py                  # mock_services(), MockAsyncServices
│   ├── test_cli.py                 # Typer commands — query, upsert, chat (mocked)
│   ├── test_verify.py              # verify_model(), verify_all(), schema register (mocked)
│   ├── test_auth_codes.py          # OAuth auth code parsing — JSONB double-encoding resilience
│   ├── test_mcp_oauth_flow.py      # Full MCP OAuth 2.1 flow: register → authorize → callback → token
│   └── test_stripe_webhooks.py     # Stripe webhook handlers — refunds, past_due, idempotency
├── integration/                    # Integration tests — live Postgres
│   ├── conftest.py                 # Integration-specific fixtures (clean_db, etc.)
│   ├── database/                   # Database & storage layer
│   │   ├── test_database.py        # Bootstrap, triggers, rem_* functions, session clone/search
│   │   ├── test_repository.py      # Repository.upsert() across all 13 entity models
│   │   ├── test_kv_store.py        # KV triggers, normalize_key(), indexes, rebuild
│   │   └── test_query_engine.py    # RemQueryParser, dispatch, SQL safety, live roundtrips
│   ├── memory/                     # Memory & moments
│   │   ├── test_memory.py          # MemoryService — persist/load, compaction, encryption
│   │   ├── test_moments.py         # Moment building, chaining, timeline, today summary
│   │   ├── test_moments_feed.py    # rem_moments_feed — daily summaries, pagination, scoping
│   │   └── test_memory_pipeline.py # End-to-end: compaction → breadcrumbs → replay
│   ├── agents/                     # Agents & chat
│   │   ├── test_chat.py            # Chat endpoint, AgentAdapter, config, routing, streaming
│   │   ├── test_agent_tools.py     # Agent construction, MCP tools, delegation, event sink
│   │   ├── test_tools.py           # MCP tool implementations (search, action, user_profile)
│   │   └── test_remind_me.py       # remind_me tool — pg_cron scheduling, one-time/recurring
│   ├── security/                   # Encryption, auth, MCP token exchange
│   │   ├── test_encryption.py      # Encryption modes, tenant isolation, deterministic, own-key
│   │   ├── test_vault.py           # VaultTransitKMS (requires OpenBao on localhost:8200)
│   │   ├── test_auth_flows.py      # JWT, refresh rotation, magic links, OAuth callbacks
│   │   └── * test_mcp_token_exchange.py  # ← SEE NOTE BELOW
│   ├── content/                    # Content & embeddings
│   │   ├── test_content.py         # ContentService — ingest, chunk, extract (all mocked)
│   │   └── test_embeddings.py      # Embedding pipeline, providers, queue worker, API
│   ├── api/                        # API & CLI
│   │   ├── test_api.py             # FastAPI endpoints — health, schemas, queries, upsert chain
│   │   └── test_cli.py             # Typer commands — live DB integration for query roundtrips
│   ├── ontology/                   # Schema verification & ingestion
│   │   ├── test_verify.py          # verify_model/all, schema register, CLI, live integration
│   │   ├── test_links.py           # Link verification in ontology files
│   │   └── test_upsert.py          # Ontology ingestion — markdown, structured, resource paths
│   ├── dreaming/                   # Dreaming agent
│   │   ├── test_dreaming.py        # Dreaming handler — context building, agent invocation
│   │   ├── test_dreaming_e2e.py    # End-to-end dreaming with FunctionModel — phases 1-3
│   │   ├── test_save_moments.py    # save_moments tool — dream moments, graph edges, back-edges
│   │   └── test_merge_graph_edges.py # Graph edge merging logic
│   └── fixtures/                   # Seed data validation
│       └── test_seed_jamie.py      # Jamie Rivera seed data — feed structure, card variants
└── .sims/                          # Simulations — demos, diagnostics, scripted workflows
    └── ...
```

---

## `*` test_mcp_token_exchange.py — Critical Integration Test

**Why this test exists:** The MCP OAuth 2.1 token exchange involves multiple layers (kv_store auth codes, PKCE verification, user lookup via Repository, JWT issuance) and each layer had a different bug that only manifested in production with real data:

1. **asyncpg JSONB double-encoding** — The DB pool registers `json.dumps` as the JSONB codec. When SQL used `$1::jsonb`, asyncpg double-encoded the parameter, causing PostgreSQL's `||` operator to produce an array `[{original}, "double-encoded-string"]` instead of a merged object. Auth codes became unreadable.
2. **`devices` column not in `_JSONB_COLUMNS`** — `Repository._decrypt_row()` only parses JSON strings for columns listed in `_JSONB_COLUMNS`. The `devices` column was missing, so `User.model_validate()` received a raw JSON string instead of a list, causing a Pydantic validation error during token exchange.

Both bugs were invisible in unit tests (mocked DB) and only appeared when a real user with push notification devices tried to authenticate via MCP (Claude Desktop). This integration test reproduces the exact production scenario: creates a user with devices stored as a JSON string in the DB, then runs the full PKCE token exchange to verify the complete chain works.

**Tests:**
- `test_full_exchange_with_devices` — The exact production failure: user.devices is a JSON string in DB
- `test_exchange_without_devices` — Basic exchange without devices
- `test_exchange_bad_verifier_fails` — Wrong PKCE code_verifier is rejected
- `test_code_single_use` — Auth code is consumed after first exchange

---

## Unit Tests

No database required. All services mocked via `tests/unit/helpers.py`.

| Test | What it covers |
|------|----------------|
| `test_cli` | Typer CLI commands — `query` (one-shot modes, table format, errors), `upsert` (JSON/YAML/Markdown/directories), `chat` (session creation, agent selection, delegation). All mocked |
| `test_verify` | `verify_model()` checks (missing table/column, extra columns, embedding tables, triggers, schema metadata), `verify_all()` iteration, `_build_json_schema`, `_derive_kv_summary`, CLI commands |
| `test_auth_codes` | OAuth auth code parsing — normal JSON, JSONB double-encoded array recovery, `set_authorization_code_user` writes plain TEXT (no `::jsonb` cast), `consume_authorization_code` handles both formats |
| `test_mcp_oauth_flow` | End-to-end MCP OAuth 2.1 flow with mocked AuthService: client registration (DCR), authorize + Google redirect, callback with code redirect, token exchange with PKCE, missing params error, bad PKCE rejection |
| `test_stripe_webhooks` | Stripe webhook handlers — `invoice.payment_failed` marks past_due, idempotent duplicate detection, `charge.refunded` reverses addon credits, unknown payment_intent warning, subscription refund no-op, `checkout.session.completed` populates payment_intents, unknown price defaults to free |

## Integration Tests

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

Security layer — envelope encryption, KMS backends, OAuth, JWT, magic links, MCP token exchange.

| Test | What it covers |
|------|----------------|
| `test_encryption` | Encryption modes via Repository — platform (transparent), client (ciphertext returned), deterministic (email search), tenant isolation, disabled mode, system key fallback, own-key |
| `test_vault` | `VaultTransitKMS` integration — platform/client modes, system key fallback, tenant isolation. **Requires OpenBao/Vault on localhost:8200** |
| `test_auth_flows` | `AuthService` — JWT create/verify/expiry, refresh rotation, revocation, magic link full flow (create → verify → single-use), Google/Apple OAuth callbacks |
| `* test_mcp_token_exchange` | **Critical.** Full MCP OAuth 2.1 token exchange against real DB — PKCE flow, devices JSON string parsing, single-use codes. Catches the production bugs described above |

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
| `test_cli` | Typer commands — `query` (live DB roundtrips), `upsert`, `chat` integration |

### dreaming/ — Dreaming Agent

Background reflective agent that generates dream moments from recent activity.

| Test | What it covers |
|------|----------------|
| `test_dreaming` | Dreaming handler — context building from recent sessions, agent invocation with mocked model, moment persistence |
| `test_dreaming_e2e` | End-to-end dreaming with `FunctionModel` — all 3 phases (reflect, search, save), graph edge creation |
| `test_save_moments` | `save_moments` MCP tool — dream moment creation, affinity→graph_edges, bidirectional back-edges on targets, graceful missing target |
| `test_merge_graph_edges` | Graph edge merging — dedup, weight update, empty lists, field preservation, default relation |

### ontology/ — Schema Verification & Ingestion

| Test | What it covers |
|------|----------------|
| `test_verify` | `verify_model()` checks (missing table/column, extra columns, embedding tables, triggers, schema metadata), `verify_all()` iteration, CLI `schema verify` / `schema register`, live integration (clean DB pass, idempotent register) |
| `test_links` | Link verification — extract links from markdown, verify local/external links, DB-backed entity link validation |
| `test_upsert` | Ontology ingestion — markdown, structured (JSON/YAML), resource upsert paths |

### fixtures/ — Seed Data Validation

| Test | What it covers |
|------|----------------|
| `test_seed_jamie` | Jamie Rivera seed data — feed structure validation, card variants |

## .sims/ — Simulations

The `.sims/` directory holds scripted scenarios that are test-like but serve a different purpose — demos, diagnostics, and end-to-end workflow replays. They exercise the full stack (real database, real agents, real MCP tools, real memory compaction) but are not part of the regular `pytest` suite.

Use sims to: demonstrate a feature to stakeholders, diagnose a production issue by replaying a workflow, or validate a cross-cutting scenario that spans multiple services (e.g. create a user → upload documents → chat with an agent → verify moments → resume session).

```bash
# Run a specific sim directly
uv run python tests/.sims/onboarding_demo.py
```
