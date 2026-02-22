# Embedding Service

Generates vector embeddings for entity content fields. These embeddings power [SEARCH](search) queries — semantic similarity matching via pgvector.

## Providers

| Provider | Model | Dimensions |
|----------|-------|------------|
| `openai` | text-embedding-3-small | 1536 |
| `fastembed` | BAAI/bge-small-en-v1.5 | 384 |
| `local` | Hash-based (test only) | 1536 |

## How it works

1. Entity upserted with an embeddable field (e.g., `content` on ontologies)
2. Database trigger queues the entity in `embedding_queue` (UNLOGGED)
3. Embedding worker polls the queue, batches texts, calls the provider
4. Resulting vectors stored in `embeddings_<table>` companion table
5. [SEARCH](search) queries use pgvector's HNSW index for fast similarity

## Which fields are embedded

Each entity type declares `__embedding_field__`:

| Entity | Field | Why |
|--------|-------|-----|
| Ontology | `content` | Domain knowledge for semantic retrieval |
| Resource | `content` | Document chunks |
| Schema | `description` | Agent/model descriptions |
| Moment | `summary` | Consolidated activity summaries for search |
| Session | `description` | Session metadata for search |

Messages are **not** embedded individually — they are consolidated into moments first, which are then embedded. This avoids the cost of embedding every message while keeping activity searchable.

## Size constraints

Ontology pages should be < 500 tokens (~2000 chars) for precise embeddings. Larger content should be split into linked pages or ingested as resources (which are chunked automatically).

## Related

- [SEARCH](search) — uses embeddings for similarity queries
- [KV Store](kv-store) — complements embeddings with O(1) key lookup
- [REM Overview](overview) — all query modes
