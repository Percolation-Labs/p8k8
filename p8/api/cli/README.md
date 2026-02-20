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

Run database bootstrap scripts (`install_entities.sql` + `install.sql`).

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
- **Resources** support chunking by convention (file → chunks via content service). TODO when `services/content.py` lands.
- **JSON/YAML** → explicit table name required. Data validated against the model class.

By default, rows are **public** (no tenant_id or user_id). Only use `--tenant-id` / `--user-id` if you need data to be private — tenant-scoped rows are encrypted at rest and filtered by tenant in all queries.

```bash
p8 upsert docs/ --tenant-id acme     # private to tenant "acme"
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

## Architecture

```
api/cli/
├── __init__.py     # Typer app + subcommand registration
├── serve.py        # p8 serve
├── migrate.py      # p8 migrate
├── query.py        # p8 query
├── upsert.py       # p8 upsert
├── schema.py       # p8 schema list/get/delete/verify/register
└── chat.py         # p8 chat
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
