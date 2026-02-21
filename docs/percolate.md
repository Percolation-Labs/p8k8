# Percolate & RemDB

## Percolation Labs

Percolation Labs builds agentic memory infrastructure. The core thesis is that intelligence belongs in the data tier, not the application tier. Rather than treating databases as dumb stores with AI bolted on top, Percolation Labs embeds semantic reasoning, tool orchestration, and memory management directly into PostgreSQL.

The name reflects the organizing metaphor: ideas, relationships, and patterns gradually **percolate** through a system — accumulating, forming connections, and reorganizing into larger coherent structures over time.

## Percolate

Percolate is a design philosophy and system architecture for building AI agents with persistent, evolving memory. It treats memory and learning as fundamentally **diffuse and generative** processes rather than static retrieval operations.

### The core insight

Traditional RAG (Retrieval-Augmented Generation) is stateless — each query is isolated, and the system never learns. Percolate rejects this model. Instead, it draws on how biological memory actually works:

- **Memory consolidates over time.** Short-term interactions are progressively distilled into durable knowledge through background processing ("dreaming"), much like how sleep consolidates episodic memory into long-term storage.
- **Knowledge is a living graph.** Entities accumulate relationships through use. The system discovers connections that were never explicitly defined — patterns emerge from the data, not from upfront schema design.
- **Learning is generative, not extractive.** The system doesn't just retrieve what was stored. It synthesizes new insights by finding affinities between distant pieces of knowledge — a conversation about API design might connect to a document about ML pipelines through shared architectural principles.

### What Percolate is not

Percolate is not a chatbot framework or a thin wrapper around an LLM. It is a **data architecture** where:

- Agents are rows in a database table, not application code
- Tools are remote references (MCP/OpenAPI), not inline functions
- Memory is multi-modal (relational + vector + graph + key-value), not just embeddings
- Knowledge evolves through background processes, not just user interaction

### The percolation process

```
Raw content (conversations, documents, uploads)
    ↓
First-order consolidation — mechanical summarization into moments
    ↓
Second-order dreaming — semantic search finds cross-domain connections
    ↓
Graph edges link new insights to older knowledge
    ↓
Mature knowledge graph — answers emerge from accumulated structure
```

Each cycle enriches the graph. A question that couldn't be answered yesterday may become answerable today because a new connection was discovered overnight. This is percolation — knowledge filtering through layers, gaining structure and meaning as it goes.

## RemDB

RemDB is the database interface layer that makes Percolate possible. It is a query abstraction over PostgreSQL that unifies four access patterns into a single system.

### Why one database

Most AI systems cobble together a vector database, a graph database, a key-value store, and a relational database. RemDB collapses all four into PostgreSQL with extensions:

- **pgvector** for embedding similarity search
- **pg_trgm** for fuzzy text matching
- **JSONB** for graph edges and flexible metadata
- **UNLOGGED tables** for O(1) key-value lookups

One system means ACID guarantees, simpler operations, and no data synchronization problems.

### REM query language

RemDB exposes a purpose-built query dialect with predictable performance characteristics:

| Query | Use | How it works |
|-------|-----|-------------|
| `LOOKUP key` | Exact entity retrieval | O(1) via cached key-value store |
| `SEARCH "text" FROM table` | Semantic similarity | Vector distance via pgvector |
| `FUZZY "text"` | Partial/typo-tolerant matching | Trigram similarity via pg_trgm |
| `TRAVERSE key DEPTH n` | Follow relationships | Recursive graph walk via JSONB edges |
| `SQL ...` | Arbitrary queries | Full PostgreSQL |

Queries are dispatched to PostgreSQL functions (`rem_lookup`, `rem_search`, `rem_fuzzy`, `rem_traverse`) that push computation into the database rather than pulling data into the application.

### Entity model

Every entity in RemDB — resources, moments, users, ontologies — shares a common foundation:

- **Identity** — UUID, timestamps, soft deletion
- **Ownership** — user and tenant scoping for isolation
- **Graph edges** — JSONB array of weighted, typed relationships to other entities
- **Metadata** — flexible JSONB for domain-specific data
- **Tags** — classification labels
- **Embeddings** — one or more vector embeddings per entity, stored in companion tables

Graph edges use human-readable keys (`sarah-chen`, `q4-report-chunk-0000`) rather than UUIDs, making the knowledge graph navigable in conversation.

### Key-value cache

An UNLOGGED table (`kv_store`) provides O(1) lookups by entity name. It's populated automatically by database triggers when entities are created or updated. UNLOGGED means no write-ahead log overhead — fast but rebuilt from primary tables on restart.

### How queries evolve with data maturity

RemDB's query modes become more powerful as the knowledge graph grows:

| Data maturity | Available queries |
|---------------|-------------------|
| Raw content only | LOOKUP, FUZZY, SQL |
| After consolidation (moments exist) | + temporal filtering, tag queries |
| After dreaming (graph edges exist) | + SEARCH, TRAVERSE |
| Mature graph | All queries, rich multi-hop traversal |

This progression mirrors the percolation concept — the system starts with raw data and gradually builds structure that enables increasingly sophisticated retrieval.
