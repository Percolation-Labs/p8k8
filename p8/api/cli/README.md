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
p8 query 'LOOKUP "sarah-chen"'
p8 query 'SEARCH "database" FROM schemas LIMIT 5'
p8 query 'FUZZY "sara" LIMIT 10'
p8 query 'SELECT name, kind FROM schemas LIMIT 5'
p8 query --format table 'FUZZY "agent"'
p8 query                        # interactive REPL
```

### upsert

Bulk upsert from files. Markdown defaults to `ontologies`. JSON/YAML requires an explicit table.

```bash
p8 upsert docs/architecture.md               # .md → ontologies (default)
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
├── encryption.py   # p8 encryption status/configure/test
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
| `dream` | `DreamingHandler.handle()` (Phase 1 + Phase 2) |
| `mcp` | `bootstrap_services()` + `init_tools()` + `FastMCP.run_async(stdio)` |
| `verify-links` | `utils.links.verify_links()` |
