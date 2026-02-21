# TRAVERSE

Recursive graph walk via `graph_edges` JSONB. Starting from a known entity, follows links to discover connected entities up to a configurable depth.

## Function signature

```sql
rem_traverse(p_entity_key, p_tenant_id DEFAULT NULL, p_user_id DEFAULT NULL,
             p_max_depth DEFAULT 1, p_rel_type DEFAULT NULL, p_keys_only DEFAULT FALSE)
```

## How it works

1. Seed: resolves the starting entity via [LOOKUP](lookup) in the [KV Store](kv-store)
2. Walk: reads `graph_edges` JSONB array, follows each edge's `target` to the next entity
3. Recurse: repeats up to `p_max_depth`, avoiding cycles via path tracking
4. Each edge has a `relation` type (e.g., `related`, `chunk_of`, `references`) and optional `weight`

## Graph edges format

Stored as a JSONB array on every entity:

```json
[
  {"target": "rem-search", "relation": "related", "weight": 1.0},
  {"target": "kv-store", "relation": "depends_on", "weight": 0.8}
]
```

Edge targets are entity keys resolvable via [LOOKUP](lookup). This is how ontology markdown links (`[SEARCH](search)`) translate into traversable graph structure.

## When to use

- Discovering related knowledge starting from a known concept
- Building context by following links from a matched ontology page
- Understanding relationships between agents, tools, and knowledge

## Examples

```bash
# Follow links one level deep
p8 query 'TRAVERSE "overview" DEPTH 1'

# Walk two levels, only "related" edges
p8 query 'TRAVERSE "rem-search" DEPTH 2'
```

## Related

- [REM Overview](overview) — all query modes
- [LOOKUP](lookup) — the resolution mechanism TRAVERSE uses internally
- [SEARCH](search) — find a starting point, then TRAVERSE from there
