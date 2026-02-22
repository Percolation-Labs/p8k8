# REM — Retrieval-Enhanced Memory

REM is the query interface to p8's knowledge base. Five query modes cover different access patterns, from O(1) key lookups to recursive graph walks. All modes are available via CLI, Python, and MCP tools.

## Quick reference

| Mode | Function | Use case | Requires |
|------|----------|----------|----------|
| LOOKUP | `rem_lookup(key)` | Instant fetch by exact name | kv_store index |
| SEARCH | `rem_search(embedding, table)` | Semantic similarity | pgvector + embeddings |
| FUZZY | `rem_fuzzy(text)` | Approximate name matching | pg_trgm |
| TRAVERSE | `rem_traverse(key, depth)` | Graph walk via edges | kv_store + graph_edges |
| SQL | Direct queries | Full Postgres capability | — |

Additional functions:

| Function | Use case |
|----------|----------|
| `rem_session_timeline(session_id)` | Interleaved messages + moments for a session |
| `rem_moments_feed(user_id)` | Cursor-paginated feed of moments by date |
| `rem_load_messages(session_id)` | Flexible message loader with token/count budgets |
| `rem_build_moment(session_id)` | Atomically build an activity chunk from messages |
| `rem_persist_turn(session_id, ...)` | Persist a user+assistant turn with auto-moment |

### Turn persistence and tool calls

`rem_persist_turn` is the **fast path** — a single SQL round-trip that inserts a user message and an assistant message, updates session token totals, and optionally triggers moment building. It accepts a `p_tool_calls` JSONB column on the assistant message but does **not** create separate `tool_call` / `tool_response` rows.

When a turn involves tool calls (MCP tools, `ask_agent` delegation, structured output), `AgentAdapter.persist_turn()` takes the **slow path** instead: it inserts messages individually in order — `user` → `tool_call` → `tool_response` → ... → `assistant` — via `MemoryService.persist_message()`. Each `tool_call` row stores the call metadata (name, args, id) in `tool_calls` JSONB. Each `tool_response` row stores the result in `content`, which is especially important for `ask_agent` delegation where structured output is the artifact. Both paths live in `p8/agentic/adapter.py:persist_turn()`.

## CLI

All modes are available via `p8 query`:

```bash
p8 query 'LOOKUP "overview"'
p8 query 'SEARCH "machine learning" FROM moments LIMIT 5'
p8 query 'FUZZY "travers" LIMIT 3'
p8 query 'TRAVERSE "overview" DEPTH 2'
p8 query 'SELECT name, moment_type FROM moments LIMIT 5'
p8 query                        # interactive REPL
p8 query --format table 'FUZZY "agent"'
```

## MCP tools

When connected via MCP (Claude Code, Cursor, etc.), use the `search` tool:

```
LOOKUP "overview"
SEARCH "architecture decisions" FROM resources LIMIT 5
FUZZY "dreaming" LIMIT 3
TRAVERSE "overview" DEPTH 2
```

## LOOKUP

O(1) entity retrieval by name. The fastest mode — a single index hit on `kv_store.entity_key`.

Keys are normalized: lowercased, whitespace replaced by hyphens. `"Query Agent"` resolves as `query-agent`.

```bash
$ p8 query 'LOOKUP "overview"'
```

Returns entity data including `graph_edges`, `metadata`, and full `content`:

```json
[{
  "entity_type": "ontologies",
  "data": {
    "name": "overview",
    "content": "# REM Query System\n\nREM provides five query modes...",
    "graph_edges": [
      {"target": "lookup", "relation": "links_to", "weight": 1.0},
      {"target": "search", "relation": "links_to", "weight": 1.0},
      {"target": "fuzzy", "relation": "links_to", "weight": 1.0},
      {"target": "traverse", "relation": "links_to", "weight": 1.0},
      {"target": "kv-store", "relation": "links_to", "weight": 1.0},
      {"target": "embedding-service", "relation": "links_to", "weight": 1.0}
    ]
  }
}]
```

Use LOOKUP when you know the exact entity name — agent schemas, ontology pages, moment names, resource keys.

## SEARCH

Semantic similarity via pgvector. Embeds the query text, then finds entities whose content vectors are closest in meaning.

```bash
$ p8 query 'SEARCH "machine learning" FROM moments LIMIT 3'
```

Returns entities ranked by cosine similarity:

```json
[
  {"entity_type": "moments", "similarity_score": 0.41,
   "data": {"name": "dream-operational-synergies-between-architecture-and-ml-efficiency",
            "moment_type": "dream",
            "summary": "We discovered that operational efficiency goals permeate both..."}},
  {"entity_type": "moments", "similarity_score": 0.38,
   "data": {"name": "dream-operational-efficiency-as-a-unifying-driver-in-...",
            "moment_type": "dream"}},
  {"entity_type": "moments", "similarity_score": 0.37,
   "data": {"name": "session-ml-chunk-0", "moment_type": "session_chunk"}}
]
```

More examples:

```bash
$ p8 query 'SEARCH "how to encrypt data at rest" FROM ontologies LIMIT 5'
$ p8 query 'SEARCH "architecture decisions" FROM resources LIMIT 3'
$ p8 query 'SEARCH "summarize conversations" FROM schemas LIMIT 3'
```

Parameters: `FROM <table>` (required), `LIMIT <n>`, `MIN_SIMILARITY <0-1>` (default from `P8_EMBEDDING_MIN_SIMILARITY`, 0.3). The DB functions also default to 0.3 independently.

Searchable tables: `ontologies`, `resources`, `schemas`, `moments`, `sessions`. Messages are not embedded individually — they are searchable via their consolidated moments instead. The field searched is determined by the model's `__embedding_field__` (e.g., `summary` for moments, `content` for ontologies, `description` for schemas).

**Requires embeddings.** The query text is embedded at query time using the configured provider (default: `openai:text-embedding-3-small`). Target entities must have pre-generated embeddings in `embeddings_<table>`. Set `P8_OPENAI_API_KEY` in `.env` (with `P8_` prefix) or switch to `P8_EMBEDDING_MODEL=fastembed:BAAI/bge-small-en-v1.5` for local embedding.

## FUZZY

Approximate text matching via PostgreSQL trigrams (`pg_trgm`). Matches against both `entity_key` and `content_summary` in the kv_store.

```bash
$ p8 query 'FUZZY "travers" LIMIT 3'
```

Returns entities ranked by trigram similarity:

```json
[{
  "entity_type": "ontologies",
  "similarity_score": 0.7,
  "data": {
    "key": "traverse",
    "type": "ontologies",
    "graph_edges": [
      {"target": "lookup", "relation": "links_to", "weight": 1.0},
      {"target": "kv-store", "relation": "links_to", "weight": 1.0},
      {"target": "overview", "relation": "links_to", "weight": 1.0},
      {"target": "search", "relation": "links_to", "weight": 1.0}
    ]
  }
}]
```

FUZZY is useful for misspelled names, auto-complete, and exploring entities when you don't know the exact key. Default similarity threshold is 0.3.

## TRAVERSE

Recursive graph walk starting from a known entity. Follows `graph_edges` JSONB arrays through connected entities up to a configurable depth.

TRAVERSE has two modes:

| Mode | Syntax | Returns | Use case |
|------|--------|---------|----------|
| **Lazy** (default) | `TRAVERSE "key" DEPTH 2` | Keys, types, summaries from kv_store | Agent exploration — discover the graph shape first, then LOOKUP specific entities |
| **Load** | `TRAVERSE "key" DEPTH 2 LOAD` | Full entity rows from source tables (like LOOKUP) | Single-pass retrieval when you need all data |

**Prefer lazy mode for agents.** A depth-2 traversal can return dozens of nodes. Loading full entity rows for all of them is expensive and usually unnecessary — the agent only needs to drill into a few. Lazy mode returns keys and summaries so the agent can decide which nodes to LOOKUP in a second pass.

### Graph edges

Every entity can have a `graph_edges` JSONB array. When markdown files are ingested via `p8 upsert`, internal links (`[text](target)`) are automatically parsed into edges:

```json
[
  {"target": "lookup", "relation": "links_to", "weight": 1.0},
  {"target": "search", "relation": "links_to", "weight": 1.0}
]
```

The dreaming system also creates `builds_on` and `dreamed_from` edges between moments and source entities.

### Lazy mode (default) — keys and context

```bash
$ p8 query 'TRAVERSE "overview" DEPTH 1'
```

Returns keys, entity types, relation info, and summaries from the kv_store index. No source table joins — fast even at high depth.

```
depth=0  key=overview           rel=(root)
depth=1  key=embedding-service  rel=links_to
depth=1  key=fuzzy              rel=links_to
depth=1  key=kv-store           rel=links_to
depth=1  key=lookup             rel=links_to
depth=1  key=search             rel=links_to
depth=1  key=traverse           rel=links_to
```

The `entity_record` field contains `{summary, metadata}` from the kv_store — enough context for an agent to decide which nodes to LOOKUP. At depth 2:

```bash
$ p8 query 'TRAVERSE "overview" DEPTH 2'
```

```
depth=0  key=overview           rel=(root)
depth=1  key=embedding-service  rel=links_to
depth=1  key=fuzzy              rel=links_to
depth=1  key=kv-store           rel=links_to
depth=1  key=lookup             rel=links_to
depth=1  key=search             rel=links_to
depth=1  key=traverse           rel=links_to
depth=2  key=kv-store           rel=links_to    (from: fuzzy, lookup, traverse, search)
depth=2  key=lookup             rel=links_to    (from: fuzzy, kv-store, traverse, search)
depth=2  key=search             rel=links_to    (from: embedding-service, fuzzy, traverse)
depth=2  key=fuzzy              rel=links_to    (from: lookup, kv-store, search)
depth=2  key=traverse           rel=links_to    (from: lookup, search)
depth=2  key=embedding-service  rel=links_to    (from: search)
```

### Load mode — full entity data

```bash
$ p8 query 'TRAVERSE "overview" DEPTH 1 LOAD'
```

Adds `LOAD` to join each traversed entity to its source table, returning the full row (like LOOKUP) in `entity_record`. This is a dynamic join per entity type — more expensive but gives you everything in one pass.

Use LOAD when the graph is small and you need all data immediately. For large graphs, prefer lazy + selective LOOKUP.

### Filtering by relation type

```bash
$ p8 query 'TRAVERSE "dream-unified-boundary-validation-and-observability-patterns" DEPTH 1'
```

```
depth=0  key=dream-unified-boundary-validation-and-observability-patterns  rel=(root)
depth=1  key=dream-integration-of-api-gateway-and-ml-pipeline-boundaries   rel=builds_on
depth=1  key=dream-synergistic-boundary-enforcement-across-api-and-ml-...  rel=builds_on
```

The `TYPE` clause filters edges to a specific relation (e.g., `builds_on`, `dreamed_from`, `links_to`).

### Recommended agent pattern

```
1. TRAVERSE "starting-key" DEPTH 2       → discover graph shape (lazy, fast)
2. Review keys and summaries              → pick interesting nodes
3. LOOKUP "interesting-key"               → load full data for selected nodes
```

This two-pass approach avoids loading full entity data for nodes the agent doesn't care about.

## SQL

Full Postgres SQL for anything the REM functions don't cover.

```bash
# Recent dream moments
$ p8 query 'SELECT name, moment_type, LEFT(summary, 80) AS summary
  FROM moments WHERE deleted_at IS NULL
  ORDER BY created_at DESC LIMIT 5'
```

```json
[
  {"name": "dream-operational-efficiency-as-a-unifying-driver-in-...", "moment_type": "dream",
   "summary": "We realized that operational efficiency is a strategic driver uniting microservi"},
  {"name": "dream-coherent-sync-async-patterns-for-resilient-...", "moment_type": "dream",
   "summary": "We discovered that synchronous and asynchronous communication patterns in micros"},
  {"name": "dream-unified-boundary-validation-and-observability-...", "moment_type": "dream",
   "summary": "We synthesized that boundary validation and enforcement act as fundamental guard"}
]
```

```bash
# Entity counts by table
$ p8 query 'SELECT entity_type, COUNT(*) FROM kv_store GROUP BY entity_type ORDER BY count DESC'

# Entities with graph edges
$ p8 query "SELECT entity_key, graph_edges FROM kv_store
  WHERE graph_edges::text LIKE '%links_to%' ORDER BY entity_key LIMIT 10"

# Dream back-edges on source entities
$ p8 query "SELECT name, moment_type, graph_edges
  FROM moments WHERE graph_edges::text LIKE '%dreamed_from%'
  AND deleted_at IS NULL"
```

## Architecture

### kv_store — the index

Every entity with a `name` field is indexed in `kv_store`, an UNLOGGED table. Database triggers maintain the index on upsert. The KV store provides:

- O(1) key resolution for LOOKUP
- Trigram matching for FUZZY
- Entity type + ID resolution for TRAVERSE
- Copies of `content_summary`, `metadata`, and `graph_edges` for fast reads

UNLOGGED means no WAL writes (fast inserts) but data is lost on crash. The KV store is rebuilt from source entity tables on `p8 migrate`.

### Embeddings

Each entity table has a companion `embeddings_<table>` table storing pgvector columns. Embeddings are generated asynchronously:

1. Entity upserted → trigger queues row in `embedding_queue`
2. Embedding worker polls queue → calls provider (OpenAI, FastEmbed, or local)
3. Vector stored in `embeddings_<table>` → SEARCH can now find it

Providers:

| Provider | Model | Dimensions |
|----------|-------|------------|
| `openai` (default) | text-embedding-3-small | 1536 |
| `fastembed` | BAAI/bge-small-en-v1.5 | 384 |
| `local` | SHA-512 hash (test only) | 1536 |

### Graph edges from markdown

When markdown files are ingested via `p8 upsert`, internal links are parsed into `graph_edges`:

```markdown
See [LOOKUP](lookup) for O(1) resolution.
The [KV Store](kv-store) powers this.
```

Becomes:

```json
[
  {"target": "lookup", "relation": "links_to", "weight": 1.0},
  {"target": "kv-store", "relation": "links_to", "weight": 1.0}
]
```

External links (`http://`, `https://`, `mailto:`) are skipped. Only entity-key links are converted to edges. This makes ontology pages traversable as a knowledge graph.

## Python API

All REM functions are available as async methods on the `Database` class:

```python
from p8.services.database import Database

db = Database(settings)
await db.connect()

# LOOKUP
results = await db.rem_lookup("overview")

# SEARCH (requires embedding vector)
from p8.services.embeddings import create_provider
provider = create_provider(settings)
embedding = (await provider.embed(["machine learning"]))[0]
results = await db.rem_search(embedding, "moments", limit=5)

# FUZZY
results = await db.rem_fuzzy("travers", limit=3)

# TRAVERSE (lazy — keys + summaries)
results = await db.rem_traverse("overview", max_depth=2)

# TRAVERSE (load — full entity rows like LOOKUP)
results = await db.rem_traverse("overview", max_depth=2, load=True)

# TIMELINE
results = await db.rem_session_timeline(session_id, limit=50)

# MOMENTS FEED
results = await db.rem_moments_feed(user_id=uid, limit=20)
```

## Combining modes

A common pattern: FUZZY or SEARCH to find a starting entity, then TRAVERSE to explore its neighborhood.

```bash
# 1. Find the entity (fuzzy match)
p8 query 'FUZZY "kv stor"'
# → returns "kv-store"

# 2. Explore its connections
p8 query 'TRAVERSE "kv-store" DEPTH 1'
# → returns kv-store + linked entities (lookup, fuzzy, traverse, overview)

# 3. Drill into a specific neighbor
p8 query 'LOOKUP "traverse"'
# → returns full entity data with content and edges
```

For agents, the MCP `search` tool handles this routing — pass any REM query string and the result comes back as structured data.
