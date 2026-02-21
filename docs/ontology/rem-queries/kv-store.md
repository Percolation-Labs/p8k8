# KV Store

An UNLOGGED table providing O(1) entity resolution by normalized name. The KV store is the backbone of [LOOKUP](lookup) and [TRAVERSE](traverse) queries.

## How it works

When you upsert any entity with a `name` field, a database trigger writes a row to `kv_store`:

| Column | Source |
|--------|--------|
| `entity_key` | Normalized name (lowercase, hyphens) |
| `entity_type` | Table name (e.g., `ontologies`, `schemas`) |
| `entity_id` | Entity UUID |
| `content_summary` | COALESCE of text fields (for display) |
| `graph_edges` | Copied from the entity's `graph_edges` JSONB |
| `metadata` | Copied from the entity's `metadata` JSONB |

## Key normalization

Keys are lowercased with whitespace replaced by hyphens. `"Query Agent"` becomes `query-agent`. This means [LOOKUP](lookup) is case-insensitive.

## UNLOGGED performance

The KV store uses an UNLOGGED table — no WAL writes, so inserts are fast. The tradeoff: data is lost on crash. This is safe because the KV store is rebuilt from entity tables on startup via `p8 migrate`.

## Related

- [LOOKUP](lookup) — uses KV store for O(1) resolution
- [FUZZY](fuzzy) — searches KV store with trigram matching
- [TRAVERSE](traverse) — walks graph_edges stored in KV rows
- [REM Overview](overview) — all query modes
