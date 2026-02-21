# Ontology

Domain knowledge stored as small markdown pages in the `ontologies` table. Each page is embedded for semantic search and linked to other pages via `[key](target)` markdown links that form a navigable knowledge graph.

## Why an ontology?

Agents need structured domain knowledge — not giant documents, but small focused pages they can look up by name or find by meaning. The ontology is that knowledge base:

- **Semantic search** finds pages by meaning (`SEARCH "how do I query entities?"`)
- **Key lookup** resolves a page instantly by name (`LOOKUP "rem-search"`)
- **Graph traversal** follows links between pages (`TRAVERSE "rem-overview" DEPTH 2`)
- **Fuzzy matching** finds pages by approximate name (`FUZZY "searh"`)

Every markdown file becomes one row in `ontologies` (name = filename stem, content = file body). The `content` field is embedded automatically — so each file **must** be small enough for the embedding model (< 500 tokens, roughly 2000 characters).

## Link conventions

Use standard markdown links where the target is an entity key (the filename stem of another ontology page or any entity name in the KV store):

```markdown
See [REM Search](rem-search) for semantic similarity queries.
Related: [Ontology Model](ontology-model), [Embedding Service](embedding-service).
```

The link text is human-readable. The link target is the **entity key** — the same string you'd pass to `LOOKUP`. Links serve double duty:

1. Readable cross-references in the markdown
2. Graph edges extracted by `p8 verify-links` and resolvable via `rem_traverse()`

When you upsert ontology pages, the KV store trigger indexes each page by its `name` field. Links pointing to other ontology page names (or any entity key) can be resolved at query time via `rem_lookup()` or walked via `rem_traverse()`.

## Categories

Ontology pages are organized by topic. Each category is a subfolder:

| Category | Folder | What goes here |
|----------|--------|----------------|
| **Agents** | `agents/` | Agent capabilities, routing, delegation patterns |
| **Moments** | `moments/` | Temporal events, session chunks, memory compaction |
| **Security** | `security/` | Encryption, tenancy, PII redaction, auth |
| **REM Queries** | `rem-queries/` | Query modes, functions, usage patterns |

## File size rule

Every markdown file must fit within the embedding model's token limit. For `text-embedding-3-small` (default), that's ~8,191 tokens — but we target **< 500 tokens** (~2000 chars) per page. This keeps semantic search precise: one page = one concept = one embedding vector.

If your content exceeds this, split it into multiple linked pages:

```
# Too big — split into focused pages
security/encryption-overview.md    → high-level design
security/envelope-encryption.md    → DEK/KEK mechanics
security/tenant-modes.md           → platform/client/sealed modes
```

## Ingesting an ontology

```bash
# Upsert a single page
p8 upsert docs/ontology/rem-queries/overview.md

# Upsert an entire folder (all .md files recursively)
p8 upsert docs/ontology/rem-queries/

# Upsert everything
p8 upsert docs/ontology/

# With tenant isolation
p8 upsert docs/ontology/ --tenant-id acme-corp
```

Markdown files default to the `ontologies` table. Each file becomes one row:
- `name` = filename stem (e.g., `overview` from `overview.md`)
- `content` = full file body
- `id` = deterministic UUID from `(name, user_id)` — upserts are idempotent

## Verifying links

After ingesting, verify that all `[text](target)` links in your ontology resolve to known entity keys:

```bash
p8 verify-links docs/ontology/
```

This scans all markdown files, extracts link targets, and checks each one against either:
1. Another markdown file in the ontology tree (by stem name)
2. An entity in the KV store (requires a running database)

Broken links are reported with file path and line number.

## Querying ontology pages

Once ingested, ontology pages are available through all REM query modes:

```bash
# Key lookup — O(1) by name
p8 query 'LOOKUP "rem-search"'

# Semantic search — find relevant pages by meaning
p8 query 'SEARCH "how to find similar content" FROM ontologies LIMIT 5'

# Fuzzy match — approximate name matching
p8 query 'FUZZY "seach" LIMIT 5'

# Graph traversal — follow links between pages
p8 query 'TRAVERSE "rem-overview" DEPTH 2'
```

Agents use these same queries at runtime to pull relevant knowledge into their context window.
