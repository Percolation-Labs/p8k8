# LOOKUP

Instant entity retrieval by name. Uses the [KV Store](kv-store) for O(1) resolution — the fastest query mode.

## Function signature

```sql
rem_lookup(p_entity_key, p_tenant_id DEFAULT NULL, p_user_id DEFAULT NULL)
```

## How it works

1. The input key is normalized (lowercased, whitespace-to-hyphens)
2. A single index lookup on `kv_store.entity_key` returns the entity
3. The result includes `id`, `type`, `summary`, `metadata`, and `graph_edges`

## When to use

- Resolving a known entity by name (e.g., agent names, ontology page keys)
- Following [graph edges](traverse) — each edge target is a key you can LOOKUP
- Checking if an entity exists before upserting

## Examples

```bash
# Lookup an ontology page
p8 query 'LOOKUP "rem-search"'

# Lookup an agent schema
p8 query 'LOOKUP "query-agent"'

# Lookup with tenant scope
p8 query 'LOOKUP "customer-data" --tenant-id acme'
```

## Related

- [REM Overview](overview) — all query modes
- [TRAVERSE](traverse) — follows LOOKUP targets through graph edges
- [FUZZY](fuzzy) — when you don't know the exact key
