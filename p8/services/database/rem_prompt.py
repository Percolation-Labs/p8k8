"""LLM prompt template for natural language → REM dialect translation.

Two parts:
  - **Static**: BNF grammar, mode selection heuristics, examples
  - **Dynamic**: ``build_rem_prompt(db)`` queries the database for available tables
    and their columns, injecting live schema info into the prompt
"""

from __future__ import annotations

REM_GRAMMAR = r"""
## REM Query Dialect — Syntax Reference

### BNF Grammar

```
<query>        ::= <lookup> | <search> | <fuzzy> | <traverse> | <sql>

<lookup>       ::= "LOOKUP" <key_list>
<key_list>     ::= <key> ("," <key>)*
<key>          ::= <quoted_string> | <bare_word>

<search>       ::= "SEARCH" <query_text> <search_clause>*
<search_clause>::= "FROM" <table_name>
                 | "FIELD" <field_name>
                 | "LIMIT" <integer>
                 | "MIN_SIMILARITY" <float>

<fuzzy>        ::= "FUZZY" <query_text> <fuzzy_clause>*
<fuzzy_clause> ::= "THRESHOLD" <float>
                 | "LIMIT" <integer>

<traverse>     ::= "TRAVERSE" <start_key> <traverse_clause>*
<traverse_clause> ::= "DEPTH" <integer>
                    | "TYPE" <rel_type>

<sql>          ::= "SQL" <raw_sql_string>
                 | <raw_sql_string>

<query_text>   ::= <quoted_string> | <bare_word>+
<quoted_string>::= '"' <chars> '"'
<bare_word>    ::= [^\s"]+
<integer>      ::= [0-9]+
<float>        ::= [0-9]+ ("." [0-9]+)?
```

### Mode Selection Guide

| User Intent | Mode | Example |
|---|---|---|
| Find a specific named entity | LOOKUP | `LOOKUP "sarah-chen"` |
| Find entities by meaning/topic | SEARCH | `SEARCH "machine learning" FROM ontologies LIMIT 5` |
| Find entities by approximate name | FUZZY | `FUZZY "sara chen" LIMIT 5` |
| Explore relationships/connections | TRAVERSE | `TRAVERSE "sarah-chen" DEPTH 2` |
| Complex filtering or aggregation | SQL | `SQL SELECT name, kind FROM schemas WHERE kind = 'agent'` |

### Heuristics

- **Exact name / identifier** → LOOKUP (fastest, O(1) via KV store)
- **Conceptual / semantic question** → SEARCH (vector similarity, needs FROM table)
- **Misspelled / partial name** → FUZZY (trigram matching)
- **"What is connected to X"** → TRAVERSE (graph walk)
- **Counting, grouping, filtering by column values** → SQL

### Examples

```
# Exact entity lookup (supports comma-separated multi-key)
LOOKUP "sarah-chen"
LOOKUP "sarah-chen", "project-atlas"

# Semantic search — finds conceptually similar content
SEARCH "database migration best practices" FROM ontologies LIMIT 5
SEARCH "authentication" FROM schemas FIELD content MIN_SIMILARITY 0.6

# Fuzzy name matching — tolerant of typos
FUZZY "sara chen" LIMIT 10
FUZZY "projct atls" THRESHOLD 0.2

# Graph traversal — explore connections
TRAVERSE "sarah-chen" DEPTH 2
TRAVERSE "project-atlas" DEPTH 1 TYPE "member"

# Raw SQL — full access for complex queries
SQL SELECT name, kind FROM schemas WHERE kind = 'agent' ORDER BY created_at DESC
SQL SELECT COUNT(*) FROM ontologies WHERE tenant_id = 'acme'
```

### Key Rules

1. Keys in LOOKUP/TRAVERSE are kebab-case normalized (e.g. "Sarah Chen" → "sarah-chen")
2. SEARCH requires a FROM clause to specify which table to search — default is "schemas"
3. FUZZY searches across the KV store (all entity types)
4. SQL mode blocks destructive statements (DROP, TRUNCATE, ALTER, DELETE without WHERE)
5. Quoted strings preserve spaces; unquoted tokens are joined
""".strip()

SYSTEM_PROMPT_TEMPLATE = """You are a query translator. Convert the user's natural language question into a REM dialect query.

{grammar}

{table_info}

### Instructions

- Output ONLY the REM query — no explanation, no markdown fences, no preamble.
- Choose the simplest mode that answers the question.
- For entity lookups by name, prefer LOOKUP (fastest).
- For semantic/conceptual questions, use SEARCH with the most relevant table.
- For misspelled or partial names, use FUZZY.
- For relationship questions ("who works with X", "what connects to Y"), use TRAVERSE.
- For counting, aggregation, or column-based filtering, use SQL.
- Normalize entity names to kebab-case for LOOKUP and TRAVERSE keys.
- Always include FROM clause for SEARCH queries.
"""


async def build_rem_prompt(db) -> str:
    """Build a complete system prompt with dynamic table info from the database.

    Queries ``schemas WHERE kind='table'`` to discover available tables and
    their columns, then injects this into the static prompt template.

    Parameters
    ----------
    db : Database
        Connected database instance with ``fetch()`` method.

    Returns
    -------
    str
        Complete system prompt for LLM-based query translation.
    """
    table_info = await _fetch_table_info(db)
    return SYSTEM_PROMPT_TEMPLATE.format(
        grammar=REM_GRAMMAR,
        table_info=table_info,
    )


async def _fetch_table_info(db) -> str:
    """Query the database for registered table schemas and format them."""
    rows = await db.fetch(
        "SELECT name, description, json_schema FROM schemas WHERE kind = 'table'"
        " AND deleted_at IS NULL ORDER BY name"
    )

    if not rows:
        return "### Available Tables\n\nNo table schemas registered."

    lines = ["### Available Tables\n"]
    for row in rows:
        name = row["name"]
        desc = row.get("description") or ""
        schema = row.get("json_schema") or {}

        # Extract column names from JSON Schema properties
        props = schema.get("properties", {})
        columns = sorted(props.keys()) if props else []

        line = f"- **{name}**"
        if desc:
            line += f" — {desc}"
        if columns:
            line += f"\n  Columns: {', '.join(columns)}"
        lines.append(line)

    return "\n".join(lines)
