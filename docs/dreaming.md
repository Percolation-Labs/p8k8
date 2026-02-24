# Dreaming

Background AI reflection on user activity. The dreaming system periodically reviews recent conversations, moments, and resources to surface cross-session insights and connections that might not be noticed in the flow of conversation.

## CLI

```bash
# Run dreaming for a user (default: last 24 hours)
p8 dream <user-id>

# Custom lookback window (last 7 days)
p8 dream <user-id> --lookback 7

# Exploration mode — dream even with no recent activity
p8 dream <user-id> --allow-empty

# Write full results (moments + back-edges) to YAML
p8 dream <user-id> -o /tmp/dreams.yaml
```

> **Validation**: All changes to the dreaming system MUST be validated by running
> `p8 dream <user-id>` (or `p8 dream --simulate` for the full
> seeded test harness) and confirming:
> 1. Session messages — correct role counts (user, assistant, tool_call), no failed searches
> 2. Structured output — `dream_moments` populated with `affinity_fragments` (not empty)
> 3. Database moments — `moments` table has rows with `moment_type='dream'` and non-empty `graph_edges`
> 4. Back-edges — referenced entities in source tables (`resources`, `moments`) have `dreamed_from` edges
> 5. Usage tracking — `usage_tracking` row for `dreaming_io_tokens` matches `result.usage().total_tokens`

## Two Orders of Dreaming

Dreaming operates in two distinct phases, inspired by how biological memory consolidation works:

**First-order dreaming** consolidates recent short-term memory into persistent memory chunks. This is purely mechanical — `rem_build_moment()` SQL function scans recent sessions and creates `session_chunk` moments that summarize conversation segments. No LLM involved. The output is a set of structured moments with summaries, tags, and basic graph edges.

**Second-order dreaming** is the creative phase. An LLM agent reads the freshly consolidated moments alongside recent messages and resources, then casts a wide net across the **full knowledge base** to find older moments and resources with semantic affinity. It searches moments (past conversation summaries) and resources (uploaded files and documents) separately — these are different data and both valuable. The agent links new insights to older knowledge via `affinity_fragments`, creating graph edges that enrich the knowledge graph over time.

The key insight: first order loads and generates, second order searches and connects. The agent uses **structured output** — its `dream_moments` field IS the result. After the agent completes, the handler persists each `DreamMoment` directly to the database and merges back-edges onto referenced entities. No tool call needed for persistence.

## Trigger

`enqueue_dreaming_tasks()` runs via pg_cron every hour (`0 * * * *`). It finds users with new activity since their last dreaming run using a UNION of two paths: (1) new messages via `users → sessions → messages`, and (2) new completed file uploads via `users → files` (where `processing_status = 'completed'`). Both paths are compared against the most recent `task_queue` entry of type `dreaming`. Users who already have a pending/processing dreaming task are skipped.

```sql
INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload)
VALUES ('dreaming', 'small', user_id, tenant_id,
        '{"trigger": "scheduled", "enqueued_at": "..."}');
```

## Execution

A worker claims the task via `QueueService.claim("small", worker_id)`. Before processing, `check_task_quota()` runs a **pre-flight** check on `dreaming_minutes` to enforce plan limits. If the user is over quota, the task is skipped.

### Phase 1 — First-order dreaming (consolidation + resource enrichment)

`DreamingHandler._build_session_moments()` finds the 10 most recently updated sessions for the user (`ORDER BY updated_at DESC LIMIT 10`, excluding `mode='dreaming'`) and calls `rem_build_moment(session_id, tenant_id, user_id, 6000)` for each. This SQL function creates `session_chunk` moments that summarize conversation segments exceeding the token threshold. No LLM, no API tokens — purely SQL text processing.

After each moment is built, `_enrich_moment_with_resources()` checks whether the session has any `content_upload` moments. If so, it extracts `chunk-0000` resource keys from their metadata, queries the `resources` table for content, and appends an `[Uploaded Resources]` section to the moment summary (each resource truncated to 500 chars). The `resource_keys` are also merged into the moment's metadata for downstream use. This ensures file uploads are visible in session_chunk consolidation without modifying the `rem_build_moment()` SQL function.

### Phase 2 — Second-order dreaming (semantic affinity)

`DreamingHandler._run_dreaming_agent()`:

1. **Load context** — Gathers recent activity within a date-based window (`NOW() - lookback_days`), subject to a ~38K token budget (30% of the 128K model context). Token budget is estimated via `tiktoken` to ensure the context fits:
   - Up to 50 moments (summaries, tags, graph edges)
   - Up to 5 recent sessions with up to 20 messages each (truncated to 500 chars)
   - Up to 10 recent file uploads (directly from `files` table, `processing_status = 'completed'`)
   - Up to 10 referenced resources discovered via moment `graph_edges` (truncated to 2K chars, deduplicated against already-loaded files)

2. **Create session** — A dreaming session is created with `mode='dreaming'`, `agent_name='dreaming-agent'`, named `dreaming-{user_id}`.

3. **Run agent** — The `DreamingAgent` (model: `openai:gpt-4.1-mini`, temperature: 0.7, `structured_output: true`) executes:
   - **First-order**: Read provided context, identify themes, draft 1-3 dream moments (no tool calls)
   - **Second-order**: Generate 5-10 search queries, search moments and resources separately via `SEARCH "keywords" FROM moments LIMIT 3` and `SEARCH "keywords" FROM resources CATEGORY document LIMIT 3`, discover connections to older data. Resource searches are filtered to `category='document'` (user uploads) to avoid processing auto-ingested news/digest items. In future this should filter for user content more broadly, not just by category.
   - **Output**: Populate `dream_moments`, `search_questions`, `cross_session_themes` in the structured response

4. **Persist dream moments** — The handler extracts `result.output.dream_moments` (proper Pydantic `DreamMoment` objects) and for each:
   - Converts `affinity_fragments` → `graph_edges`
   - Creates a `Moment` entity (type=`dream`) and upserts it
   - Merges `dreamed_from` back-edges onto referenced entities (see below)

5. **Persist messages** — All agent messages (user prompt, assistant responses, tool calls/results) are saved to the dreaming session.

### Back-edges — source tables, not kv_store

When a dream moment links to an existing entity (e.g. a resource or another moment) via `graph_edges`, the handler merges a `dreamed_from` back-edge onto the **source table** (`resources`, `moments`, etc.) — never directly onto `kv_store`.

`kv_store` is an UNLOGGED ephemeral index. It maps entity keys to `(entity_type, entity_id)` and caches `graph_edges` for fast lookup, but it is rebuilt from source tables by `rem_sync_kv_store()` and lost on crash. Writing back-edges to `kv_store` directly would lose them.

The flow:

1. Resolve `target_key` → `(entity_type, entity_id)` via `kv_store` (index lookup only)
2. Read current `graph_edges` from the **source table** (authoritative)
3. Merge the new `dreamed_from` edge
4. Write merged edges back to the **source table** only
5. `kv_store` picks up the change via the entity table trigger (or the next `rebuild_kv_store()` if the trigger was missed)

This means after dreaming, you can query back-edges directly on the source:

```sql
-- Resources linked to dreams
SELECT name, graph_edges FROM resources
WHERE user_id = '<uid>'
AND graph_edges @> '[{"relation": "dreamed_from"}]';

-- Moments linked to dreams
SELECT name, moment_type, graph_edges FROM moments
WHERE user_id = '<uid>'
AND graph_edges @> '[{"relation": "dreamed_from"}]';
```

### Empty activity — exploration mode

When the date window contains no activity (no messages, no uploads), dreaming normally skips. With `allow_empty_activity_dreaming` enabled in the task payload:

- **Phase 1** produces nothing — no messages to consolidate
- **Phase 2** skips first-order consolidation entirely. Instead the agent generates random semantic searches across the full knowledge base, surfacing forgotten resources, old moments, and serendipitous connections
- Output is still 1–3 `dream` moments, but born from exploration rather than consolidation

This keeps the knowledge graph alive during quiet periods and resurfaces older material.

## Token Tracking

Two kinds of token counts exist in the system — they serve different purposes:

| Type | Source | Used For |
|------|--------|----------|
| **Actual API tokens** | `result.usage().total_tokens` from pydantic-ai | Billing, quota enforcement (`usage_tracking` table) |
| **tiktoken estimates** | `estimate_tokens()` via tiktoken BPE encoder | Context budget fitting (ensuring data fits in model context window) |

Billing and quotas always use **actual API tokens** reported by the LLM provider. Estimates are only used when deciding how much data to pack into a prompt.

- Phase 1 produces **no API tokens** (SQL only, no LLM call)
- Phase 2 produces **actual API tokens** via `result.usage().total_tokens`
- Only Phase 2 tokens are tracked in `usage_tracking` as `dreaming_io_tokens`

## Usage Tracking

| Check | When | Resource Type | Location |
|-------|------|---------------|----------|
| Pre-flight | Before processing task | `dreaming_minutes` | `QueueService.check_task_quota()` |
| Post-flight | After Phase 2 completes | `dreaming_io_tokens` | `DreamingHandler.handle()` |

Plan limits for dreaming:

| Plan | `dreaming_minutes` | `dreaming_io_tokens` | `dreaming_interval_hours` |
|------|-------------------|---------------------|--------------------------|
| free | 30 | 10,000 | 24 |
| pro | 120 | 50,000 | 12 |
| team | 180 | 100,000 | 12 |
| enterprise | 360 | 500,000 | 6 |

## Structured Output

The agent uses `structured_output: true` — pydantic-ai enforces that the LLM returns a JSON object matching the `DreamingAgentOutput` schema. The handler receives proper Pydantic model instances, not raw dicts. Nested types (`DreamMoment`, `AffinityFragment`) are preserved via `AgentSchema._source_output_model`.

The agent produces structured output matching `DreamingAgent` fields:

| Field | Type | Description |
|-------|------|-------------|
| `dream_moments` | `list[DreamMoment]` | 1-3 dream moments to persist |
| `search_questions` | `list[str]` | 5-10 semantic search queries |
| `cross_session_themes` | `list[str]` | Recurring patterns as short phrases |

Each `DreamMoment` contains:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Kebab-case identifier (e.g. `dream-ml-architecture-patterns`) |
| `summary` | `str` | 2-4 sentences in shared voice ("We discovered...") |
| `topic_tags` | `list[str]` | 3-5 relevant tags |
| `emotion_tags` | `list[str]` | 0-2 emotional tones |
| `affinity_fragments` | `list[AffinityFragment]` | Links to related entities as graph edges |

## Verification

```sql
-- Dreaming sessions
SELECT id, name, created_at FROM sessions
WHERE user_id = '<uid>' AND mode = 'dreaming'
ORDER BY created_at DESC;

-- Dream moments
SELECT name, summary, topic_tags, graph_edges FROM moments
WHERE user_id = '<uid>' AND moment_type = 'dream'
ORDER BY created_at DESC;

-- Usage tracking (actual API tokens)
SELECT * FROM usage_tracking
WHERE user_id = '<uid>' AND resource_type = 'dreaming_io_tokens'
AND period_start = date_trunc('month', CURRENT_DATE)::date;

-- Back-edges on target entities
SELECT name, graph_edges FROM resources
WHERE user_id = '<uid>'
AND graph_edges @> '[{"relation": "dreamed_from"}]';
```

## Simulation

```bash
p8 dream --simulate
```

## Notes

### Session Statistics

| Metric | Value |
|--------|-------|
| **Messages** | 7 total |
| `user` | 1 (758 tokens — context prompt) |
| `assistant` | 2 (0 tokens — tool calls + final_result) |
| `tool_call` | 4 (3 lateral search results + final_result) |
| **API tokens** | 5,615 (actual, from `result.usage().total_tokens`) |
| **Message tokens** | 863 (tiktoken estimates, for session bookkeeping) |
| **Wall clock** | ~10s |
| **Search failures** | 0 out of 3 |

### Database Verification

**Moments table** — 3 rows with `moment_type='dream'`, each with 2 cross-domain `graph_edges`
- `dream-event-driven-async-communication-similarities` → `arch-doc-chunk-0000` (w=0.8) + `ml-report-chunk-0000` (w=0.6)
- `dream-pattern-validation-boundaries` → `arch-doc-chunk-0000` (w=0.7) + `ml-report-chunk-0000` (w=0.7)
- `dream-synthesis-api-gateway-microservices-ml-pipelines` → `arch-doc-chunk-0000` (w=0.9) + `ml-report-chunk-0000` (w=0.8)

**Back-edges** — Written to source tables only (`kv_store` syncs from them):
- `resources.arch-doc-chunk-0000` ← 3 `dreamed_from` edges (w=0.9, 0.7, 0.8)
- `resources.ml-report-chunk-0000` ← 3 `dreamed_from` edges (w=0.8, 0.7, 0.6)

**Usage tracking** — `dreaming_io_tokens=5615` matches Phase 2 `io_tokens` exactly.

## Example Run

```yaml
cost:
  dreaming_io_tokens: 5615
  period: '2026-02-01'
session:
  id: a026febf-3cb3-497f-919e-331c617e654a
  name: dreaming-dddddddd-0000-0000-0000-000000000001
  mode: dreaming
  agent: dreaming-agent
  total_tokens: 863
  created_at: '2026-02-21 21:19:54.022911+00:00'
dream_moments:
- name: dream-event-driven-async-communication-similarities
  moment_type: dream
  summary: We see an intriguing parallel between asynchronous microservice communication
    via NATS JetStream and event-driven triggers in ML training pipelines. Both rely
    on message queues and events to decouple components and enable scalable, incremental
    processing. This suggests an opportunity to unify tooling or monitoring strategies
    across these domains to streamline operations and observability.
  topic_tags:
  - microservices
  - asynchronous
  - event-driven
  - machine-learning
  emotion_tags: []
  graph_edges:
  - reason: The architecture document describes the use of NATS JetStream for async
      event messaging.
    target: arch-doc-chunk-0000
    weight: 0.8
    relation: thematic_link
  - reason: The ML report recommends incremental training, which often uses event-driven
      triggers.
    target: ml-report-chunk-0000
    weight: 0.6
    relation: thematic_link
  metadata:
    source: dreaming
- name: dream-pattern-validation-boundaries
  moment_type: dream
  summary: 'Our discussions reveal a recurring pattern of boundary enforcement across
    different domains: API gateways use JWT and rate limiting to enforce access control
    boundaries, while ML pipelines apply schema validation to ensure data quality. This
    pattern highlights a shared architectural principle of validating inputs at system
    edges to prevent downstream failures and maintain reliability.'
  topic_tags:
  - validation
  - boundary enforcement
  - architecture
  - machine-learning
  emotion_tags: []
  graph_edges:
  - reason: The architecture document details boundary validation via JWT and rate limiting
      at the gateway edge.
    target: arch-doc-chunk-0000
    weight: 0.7
    relation: thematic_link
  - reason: The ML report discusses schema validation as a critical quality gate in the
      pipeline.
    target: ml-report-chunk-0000
    weight: 0.7
    relation: thematic_link
  metadata:
    source: dreaming
- name: dream-synthesis-api-gateway-microservices-ml-pipelines
  moment_type: dream
  summary: We discovered a strong architectural synergy between our microservices communication
    patterns and ML data pipelines. The API gateway pattern leveraging Kong with JWT
    validation parallels the schema validation in our ML data preprocessing, both enforcing
    boundaries to ensure system integrity. Additionally, the asynchronous messaging via
    NATS JetStream mirrors event-driven triggers common in ML pipelines for incremental
    training, suggesting a unified approach to managing both service calls and data workflows.
  topic_tags:
  - microservices
  - api-gateway
  - machine-learning
  - data-pipeline
  - architecture
  emotion_tags: []
  graph_edges:
  - reason: Our synthesis builds on the detailed API gateway and communication patterns
      decided in the architecture document.
    target: arch-doc-chunk-0000
    weight: 0.9
    relation: builds_on
  - reason: The ML pipeline report emphasizes schema validation and incremental training,
      which align with the architectural principles of boundary enforcement and async
      event handling.
    target: ml-report-chunk-0000
    weight: 0.8
    relation: builds_on
  metadata:
    source: dreaming
back_edges:
  ml-report-chunk-0000:
  - target: dream-synthesis-api-gateway-microservices-ml-pipelines
    relation: dreamed_from
    weight: 0.8
    reason: The ML pipeline report emphasizes schema validation and incremental training,
      which align with the architectural principles of boundary enforcement and async
      event handling.
  - target: dream-pattern-validation-boundaries
    relation: dreamed_from
    weight: 0.7
    reason: The ML report discusses schema validation as a critical quality gate in the
      pipeline.
  - target: dream-event-driven-async-communication-similarities
    relation: dreamed_from
    weight: 0.6
    reason: The ML report recommends incremental training, which often uses event-driven
      triggers.
  arch-doc-chunk-0000:
  - target: dream-synthesis-api-gateway-microservices-ml-pipelines
    relation: dreamed_from
    weight: 0.9
    reason: Our synthesis builds on the detailed API gateway and communication patterns
      decided in the architecture document.
  - target: dream-pattern-validation-boundaries
    relation: dreamed_from
    weight: 0.7
    reason: The architecture document details boundary validation via JWT and rate limiting
      at the gateway edge.
  - target: dream-event-driven-async-communication-similarities
    relation: dreamed_from
    weight: 0.8
    reason: The architecture document describes the use of NATS JetStream for async
      event messaging.
```

## Key Files

| File | Role |
|------|------|
| `p8/agentic/core_agents.py` | `DreamingAgent` class — system prompt, structured output schema, model config |
| `p8/workers/handlers/dreaming.py` | `DreamingHandler` — first-order + second-order execution, context loading |
| `p8/services/queue.py` | `QueueService` — enqueue, claim, pre-flight quota check |
| `p8/services/usage.py` | `check_quota()`, `increment_usage()`, plan limits |
| `p8/agentic/agent_schema.py` | `AgentSchema.to_output_schema()` — preserves nested Pydantic types for structured output |
| `p8/api/tools/search.py` | MCP search tool — REM dialect (SEARCH, LOOKUP, FUZZY, TRAVERSE) |
| `p8/api/tools/save_moments.py` | MCP tool for interactive moment saving (not used by dreaming agent) |
| `sql/03_qms.sql` | `enqueue_dreaming_tasks()` SQL function, pg_cron schedule |
| `p8/utils/tokens.py` | `estimate_tokens()` — tiktoken BPE encoder for context budget fitting |
