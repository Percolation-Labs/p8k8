# TRAVERSE

Recursive graph walk via `graph_edges` JSONB. Starting from a known entity, follows links to discover connected entities up to a configurable depth.

## Function signature

```sql
rem_traverse(p_entity_key, p_tenant_id DEFAULT NULL, p_user_id DEFAULT NULL,
             p_max_depth DEFAULT 1, p_rel_type DEFAULT NULL,
             p_keys_only DEFAULT FALSE, p_load DEFAULT FALSE)
```

## Modes

| Mode | Flag | `entity_record` contains | Use case |
|------|------|--------------------------|----------|
| **Lazy** (default) | — | `{summary, metadata}` from kv_store | Agent exploration — discover graph, then LOOKUP selected nodes |
| **Load** | `p_load = TRUE` | Full entity row from source table (like [LOOKUP](lookup)) | Single-pass when you need all data |
| **Keys only** | `p_keys_only = TRUE` | `NULL` | Minimal — just graph structure |

**Prefer lazy mode for agents.** A depth-2 traversal can return many nodes — loading full rows for all of them is expensive and usually unnecessary. Use lazy to see the graph shape, then [LOOKUP](lookup) the nodes you care about.

## How it works

1. Seed: resolves the starting entity via the [KV Store](kv-store)
2. Walk: reads `graph_edges` JSONB array, follows each edge's `target` to the next entity
3. Recurse: repeats up to `p_max_depth`, avoiding cycles via path tracking
4. Each edge has a `relation` type (e.g., `links_to`, `builds_on`, `dreamed_from`) and optional `weight`

## Graph edges format

Stored as a JSONB array on every entity:

```json
[
  {"target": "rem-search", "relation": "links_to", "weight": 1.0},
  {"target": "kv-store", "relation": "links_to", "weight": 0.8}
]
```

Edge targets are entity keys resolvable via [LOOKUP](lookup). This is how ontology markdown links (`[SEARCH](search)`) translate into traversable graph structure.

## When to use

- Discovering related knowledge starting from a known concept
- Building context by following links from a matched ontology page
- Understanding relationships between agents, tools, and knowledge

## Examples

```bash
# Lazy (default) — keys + summaries
p8 query 'TRAVERSE "overview" DEPTH 1'

# Load — full entity data like LOOKUP
p8 query 'TRAVERSE "overview" DEPTH 1 LOAD'

# Depth 2, filter by relation type
p8 query 'TRAVERSE "rem-search" DEPTH 2 TYPE links_to'
```

## Recommended agent pattern

```
1. TRAVERSE "key" DEPTH 2          → discover graph shape (lazy, fast)
2. Review keys and summaries        → pick interesting nodes
3. LOOKUP "interesting-key"         → load full data for selected nodes
```

## Related

- [REM Overview](overview) — all query modes
- [LOOKUP](lookup) — the resolution mechanism TRAVERSE uses internally
- [SEARCH](search) — find a starting point, then TRAVERSE from there
