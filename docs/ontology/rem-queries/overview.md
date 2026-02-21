# REM Query System

REM (Retrieval-Enhanced Memory) provides five query modes for accessing entities stored in p8. Each mode optimizes for a different access pattern.

## Query modes

| Mode | Function | Best for |
|------|----------|----------|
| [LOOKUP](lookup) | `rem_lookup(key)` | O(1) fetch by exact name |
| [SEARCH](search) | `rem_search(embedding, table)` | Semantic similarity via pgvector |
| [FUZZY](fuzzy) | `rem_fuzzy(text)` | Approximate name matching via pg_trgm |
| [TRAVERSE](traverse) | `rem_traverse(key, depth)` | Following graph edges between entities |
| SQL | Direct queries | Full Postgres capability |

## How it works

Every entity with a `name` field is indexed in the [KV Store](kv-store) — an UNLOGGED table that provides O(1) key resolution. When you insert an ontology page named `rem-search`, a trigger writes a row to `kv_store` with `entity_key = 'rem-search'`.

Embeddings are generated asynchronously via the [Embedding Service](embedding-service). When you upsert an ontology page, the content field is queued for embedding. Once processed, `SEARCH` queries can find it by meaning.

## CLI usage

```bash
p8 query 'LOOKUP "rem-search"'
p8 query 'SEARCH "find similar documents" FROM ontologies'
p8 query 'FUZZY "searh" LIMIT 5'
p8 query 'TRAVERSE "overview" DEPTH 2'
```
