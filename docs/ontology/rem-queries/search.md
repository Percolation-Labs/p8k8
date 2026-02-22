# SEARCH

Semantic similarity search via pgvector. Finds entities whose embedded content is closest in meaning to a query, regardless of exact wording.

## Function signature

```sql
rem_search(p_query_embedding, p_table_name, p_field_name DEFAULT 'content',
           p_tenant_id DEFAULT NULL, p_provider DEFAULT 'openai',
           p_min_similarity DEFAULT 0.3, p_limit DEFAULT 10, p_user_id DEFAULT NULL)
```

## How it works

1. The query text is embedded using the configured [Embedding Service](embedding-service)
2. pgvector computes cosine distance between query embedding and stored vectors
3. Results above `p_min_similarity` threshold are returned, ordered by similarity

## When to use

- Finding relevant ontology pages for a user question
- Discovering related documents without knowing exact names
- Building agent context from the most relevant knowledge

## Examples

```bash
# Search ontologies by meaning
p8 query 'SEARCH "how to encrypt data at rest" FROM ontologies LIMIT 5'

# Search resources (document chunks)
p8 query 'SEARCH "authentication flow" FROM resources LIMIT 10'

# Search schemas for agent descriptions
p8 query 'SEARCH "summarize conversations" FROM schemas LIMIT 3'
```

## Related

- [REM Overview](overview) — all query modes
- [LOOKUP](lookup) — when you know the exact key
- [FUZZY](fuzzy) — text-based approximate matching
