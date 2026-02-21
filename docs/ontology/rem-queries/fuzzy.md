# FUZZY

Approximate text matching via PostgreSQL trigrams (pg_trgm). Finds entities whose names or summaries are similar to the query text — useful when you have a rough idea of the name but not the exact spelling.

## Function signature

```sql
rem_fuzzy(p_query, p_tenant_id DEFAULT NULL, p_threshold DEFAULT 0.3,
          p_limit DEFAULT 10, p_user_id DEFAULT NULL)
```

## How it works

1. Computes trigram similarity between the query and both `entity_key` and `content_summary` in the [KV Store](kv-store)
2. Returns entities where the best similarity score exceeds the threshold
3. Results are ordered by similarity (highest first)

## When to use

- User typed a misspelled name ("searh" matches "search")
- Exploring available entities without knowing exact names
- Auto-complete or suggestion features

## Examples

```bash
# Find entities matching an approximate name
p8 query 'FUZZY "querr agent" LIMIT 5'

# Lower threshold for broader matches
p8 query 'FUZZY "encryptin" LIMIT 10'
```

## Related

- [REM Overview](overview) — all query modes
- [LOOKUP](lookup) — when you know the exact key
- [SEARCH](search) — when you want meaning-based matching instead of text similarity
