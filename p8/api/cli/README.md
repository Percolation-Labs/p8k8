# api/cli/

Typer CLI for p8. Every command is a thin wrapper — all logic lives in `services/`.

## Install

```bash
uv sync                # installs p8 + typer + pyyaml
p8 --help         # or: python -m api.cli --help
```

## Commands

### serve

Start the API server.

```bash
p8 serve                        # 0.0.0.0:8000
p8 serve --port 9000 --reload   # dev mode
p8 serve --workers 4            # production
```

### migrate

Run database bootstrap scripts (`sql/01..04`).

```bash
p8 migrate
```

### query

Execute REM dialect queries. Without arguments, starts an interactive REPL.

```bash
p8 query 'LOOKUP percolate'
p8 query 'SEARCH "database" FROM schemas LIMIT 5'
p8 query 'FUZZY "sara" LIMIT 10'
p8 query 'SELECT name, kind FROM schemas LIMIT 5'
p8 query --format table 'FUZZY "agent"'
p8 query                        # interactive REPL
```

### upsert

Bulk upsert from files. Markdown defaults to `ontologies`. JSON/YAML requires an explicit table.

```bash
p8 upsert docs/percolate.md               # .md → ontologies (default)
p8 upsert docs/                               # folder of .md → ontologies
p8 upsert schemas data/agents.yaml            # YAML → schemas
p8 upsert resources data/chunks.json           # JSON → resources
p8 upsert servers data/servers.yaml            # YAML → servers
```

Convention:
- **Markdown** → `ontologies` by default. One Ontology per file (name = filename stem, content = file body). Ontologies are small, within embedding limits — no chunking.
- **Resources** — files ingested via ContentService: extract text, chunk, create File + Resource entities with embeddings.
- **JSON/YAML** → explicit table name required. Data validated against the model class.

By default, rows are **public** (no tenant_id or user_id). Only use `--tenant-id` / `--user-id` if you need data to be private — tenant-scoped rows are encrypted at rest and filtered by tenant in all queries.

```bash
p8 upsert docs/ --tenant-id acme     # private to tenant "acme"
```

#### Ingesting an ontology

The ontology is a folder of small markdown files (< 500 tokens each) that form a linked knowledge graph. Each file becomes one `ontologies` row with `name = filename stem` and `content = file body`.

```bash
# Upsert the full ontology
p8 upsert docs/ontology/

# Upsert one category
p8 upsert docs/ontology/rem-queries/

# Upsert a single page
p8 upsert docs/ontology/rem-queries/overview.md

# Verify all internal links resolve
p8 verify-links docs/ontology/
```

Files use `[text](target)` markdown links where the target is an entity key (another page's filename stem or any KV store key). After ingesting, pages are queryable via all REM modes — `LOOKUP` by name, `SEARCH` by meaning, `TRAVERSE` to follow links.

Three ingestion paths:

| Path | Input | Target table | Behavior |
|------|-------|-------------|----------|
| Markdown | `.md` file or folder | `ontologies` (default) | One entity per file, no chunking |
| Structured | `.yaml` / `.json` file | Explicit (e.g., `schemas`) | Validated against model, bulk upsert |
| Resources | Any other file/folder | `resources` | Extract text, chunk, create File + Resource entities |

See `docs/ontology/README.md` for the full ontology design, link conventions, and category structure.

### verify-links

Verify that markdown links in ontology files resolve to valid targets.

```bash
p8 verify-links docs/ontology/              # check against local files
p8 verify-links --db docs/ontology/         # also check KV store
```

### schema

List, inspect, delete, verify, and register schemas.

```bash
p8 schema list                       # all schemas
p8 schema list --kind agent          # agents only
p8 schema get <uuid>                 # full JSON
p8 schema delete <uuid>              # soft delete
p8 schema verify                     # check DB matches pydantic models
p8 schema register                   # sync model metadata → schemas table
```

### chat

Interactive chat with an agent. Creates a new session or resumes an existing one.

```bash
p8 chat                              # new session, default agent
p8 chat --agent query-agent          # specific agent
p8 chat --session <uuid>             # resume session
p8 chat --user-id user-123           # with user context
```

#### Commerce analytics via `commerce-analyst`

Upload data files to Percolate, then ask questions in natural language. The agent discovers your files automatically and selects the right Platoon tool.

```bash
# Upload data (use the test user ID that p8 chat defaults to)
export USER_ID="7d31eddf-7ff7-542a-982f-7522e7a3ec67"
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@products.csv"
curl -X POST http://localhost:8000/content/ \
  -H "x-user-id: $USER_ID" -F "file=@demand.csv"

# Chat — no file IDs needed, the agent finds them
p8 chat --agent commerce-analyst
```

```
you> I uploaded some commerce data. What products should I reorder first?
assistant> Binoculars (A-class, 2 days of stock, $760/day revenue) and
           Trail Camera (A-class, 1.7 days) are most urgent. 6 of 8 products
           at elevated stockout risk.

you> What does our cash situation look like for March?
assistant> Revenue: $101K. Restocking: $55K. Net cash: $7.2K. You can cover
           all reorders but cash dips on major reorder days.

you> Any unusual demand patterns around Valentine's Day?
assistant> SEED-01 dropped to 14 units on Feb 14 vs expected 26.4 (z-score -2.71).
           Customers likely shifted to gift items.
```

See [docs/commerce-analytics.md](../../../docs/commerce-analytics.md) for the full tool reference, data formats, and end-to-end case study.

### dream

Run dreaming for a user — consolidation (Phase 1) + AI insights (Phase 2).

```bash
p8 dream <user-id>                              # default: last 24 hours
p8 dream <user-id> --lookback 7                 # last 7 days
p8 dream <user-id> --allow-empty                # exploration mode (dream even with no activity)
p8 dream <user-id> -o /tmp/dreams.yaml          # write full results (moments + back-edges) to YAML
p8 dream <user-id> -l 7 -e -o /tmp/dreams.yaml  # all options combined
```

Output includes: activity chunks built (Phase 1), dream moments saved with graph edges (Phase 2), and `dreamed_from` back-edges merged onto source entities (resources, moments).

### moments

List moments, view session timelines, and trigger compaction.

```bash
p8 moments                              # today's summary + recent moments
p8 moments --type session_chunk          # filter by moment_type
p8 moments --user-id <uuid>             # filter by user
p8 moments --limit 50                   # max results
p8 moments timeline <session-uuid>      # interleaved messages + moments for a session
p8 moments timeline <session-uuid> -n 100
p8 moments compact <session-uuid>       # trigger moment compaction
p8 moments compact <session-uuid> -t 500  # custom token threshold
```

### encryption

Inspect KMS provider status, configure tenant encryption, and run round-trip tests.

```bash
p8 encryption status                              # show KMS provider + tenant keys
p8 encryption configure <tenant-id>               # configure encryption (default: platform mode)
p8 encryption configure <tenant-id> --mode client  # client-side encryption
p8 encryption configure <tenant-id> --mode sealed  # sealed mode (server-generated key)
p8 encryption configure <tenant-id> --mode disabled
p8 encryption test                                 # round-trip test (default: platform mode)
p8 encryption test --tenant my-tenant --mode client
p8 encryption test-isolation                       # verify cross-tenant decryption fails
```

### admin

Operations tooling for the processing pipeline. Defaults to remote (Hetzner via port-forward on `localhost:5491`). Use `--local` to target the local docker-compose DB.

```bash
# Health — pipeline checks + per-user task diagnostics
p8 admin health                          # all users
p8 admin health --email alice            # filter by email (partial match)
p8 admin health --user <uuid>            # filter by user UUID

# Queue — inspect task_queue
p8 admin queue                           # aggregate pending tasks by tenant/type
p8 admin queue --status failed           # aggregate failed tasks
p8 admin queue --detail                  # individual task rows
p8 admin queue --detail --type dreaming  # filter by task_type
p8 admin queue --detail --status failed -n 50  # paginated detail

# Quota — user utilization reports
p8 admin quota                           # all users
p8 admin quota --user <uuid>             # single user
p8 admin quota --user <uuid> --reset     # reset all current-period quotas
p8 admin quota --user <uuid> --reset --resource chat_tokens  # reset one resource

# Enqueue — manually enqueue a one-off task
p8 admin enqueue dreaming --user <uuid>
p8 admin enqueue news --user <uuid> --delay 30   # delay 30 minutes

# Heal — fix stale reminder cron jobs
p8 admin heal-jobs

# Env — validate .env keys are covered by K8s manifests
p8 admin env

# Sync secrets — push .env secrets into OpenBao KV v2
p8 admin sync-secrets
p8 admin sync-secrets --addr http://127.0.0.1:8200 --token <BAO_TOKEN>
```

All admin commands accept `--local` / `-L` to target the local docker-compose DB instead of remote.

### db

Compare local and remote database schemas, generate and apply migrations.

```bash
# Diff local vs remote (requires port-forward)
p8 db diff                                        # default remote port 5433
p8 db diff --remote-url postgresql://user:pass@localhost:5433/dbname
p8 db diff --tables-only                          # skip functions/triggers/indexes
p8 db diff --counts                               # include row count comparison
p8 db diff --generate                             # write sql/migrations/NNN_db_diff.sql
p8 db diff --generate -m "add_user_prefs"         # custom migration label

# Apply a migration
p8 db apply sql/migrations/001_db_diff.sql                         # apply to local
p8 db apply sql/migrations/001_db_diff.sql --remote-url <URL>      # apply to remote
p8 db apply sql/migrations/001_db_diff.sql --dry-run               # print SQL only
```

### mcp

Run the MCP server over stdio transport for local development with Claude Code, Cursor, etc.

```bash
p8 mcp                               # starts stdio MCP server
```

Configure your IDE with `.mcp.json` in the project root:

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

When `P8_MCP_AUTH_ENABLED=false`, the server binds to the Jamie Rivera test user
(`user1@example.com`) so all tool calls work without explicit auth. See `api/README.md`
for the full MCP setup and testing guide.

## Architecture

```
api/cli/
├── __init__.py     # Typer app + subcommand registration
├── serve.py        # p8 serve
├── migrate.py      # p8 migrate
├── query.py        # p8 query
├── upsert.py       # p8 upsert
├── schema.py       # p8 schema list/get/delete/verify/register
├── chat.py         # p8 chat
├── moments.py      # p8 moments / p8 moments timeline / p8 moments compact
├── dreaming.py     # p8 dream
├── encryption.py   # p8 encryption status/configure/test/test-isolation
├── admin.py        # p8 admin health/queue/quota/enqueue/heal-jobs/env/sync-secrets
├── db.py           # p8 db diff/apply
├── mcp.py          # p8 mcp (stdio transport)
└── verify_links.py # p8 verify-links
```

## Design

Every CLI command follows the same pattern:

```
typer callback → asyncio.run(_run_*()) → bootstrap_services() → service method → print
```

- `bootstrap_services()` bootstraps DB, KMS, Encryption, FileService, ContentService — same lifecycle as the API lifespan
- CLI functions call **service methods only** — no raw SQL, no business logic
- Display formatting (JSON, table) is the only CLI-specific code

## Service mapping

| CLI command | Service call |
|-------------|-------------|
| `serve` | `uvicorn.run()` |
| `migrate` | `Database.execute()` on SQL scripts |
| `query` | `Database.rem_query()` |
| `upsert` | `FileService.read_text()` + `Repository.upsert()` |
| `schema list` | `Repository.find(filters=...)` |
| `schema get` | `Repository.get()` |
| `schema delete` | `Repository.delete()` |
| `schema verify` | `verify_all(db)` |
| `schema register` | `register_models(db)` |
| `chat` | `ChatController.prepare()` + `ChatController.run_turn()` |
| `moments` | `MemoryService.build_today_summary()` + `Repository.find()` |
| `moments timeline` | `Database.rem_session_timeline()` |
| `moments compact` | `MemoryService.maybe_build_moment()` |
| `dream` | `DreamingHandler.handle()` (Phase 1 + Phase 2) |
| `encryption status` | `Database.fetch()` on `tenant_keys` |
| `encryption configure` | `Encryption.configure_tenant()` |
| `encryption test` | `Encryption.configure_tenant()` + `Repository.upsert()` round-trip |
| `admin health` | Direct SQL on `task_queue`, `cron.job`, `users` |
| `admin queue` | Direct SQL on `task_queue` (aggregate or detail) |
| `admin quota` | `usage.get_all_usage()` + `usage.get_user_plan()` |
| `admin enqueue` | `INSERT INTO task_queue` |
| `admin heal-jobs` | `_heal_reminder_jobs(db)` |
| `admin env` | Local file parsing (.env vs K8s manifests) |
| `admin sync-secrets` | OpenBao KV v2 API / `bao` CLI |
| `db diff` | `asyncpg` introspection queries on local + remote |
| `db apply` | `asyncpg.connect()` + `conn.execute()` in transaction |
| `mcp` | `bootstrap_services()` + `init_tools()` + `FastMCP.run_async(stdio)` |
| `verify-links` | `utils.links.verify_links()` |
