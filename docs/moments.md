# Moments

Moments are the core content units in the feed. Each moment has a `moment_type` that determines how it's displayed and aggregated.

## Moment Types

| Type | Source | Feed Visibility | Description |
|------|--------|-----------------|-------------|
| `today` | `rem_moments_feed()` | **Virtual** | Daily summary of all activity and chat (not stored) |
| `session_chunk` | Dreaming/compaction | Visible | Summarized chunk of activity across sessions and uploads for a time period |
| `content_upload` | File upload pipeline | Visible | Uploaded file (PDF, image, voice, etc.) |
| `notification` | `/notifications/send` | Visible | Push notification that was delivered |
| `dream` | Dreaming CronJob | Visible | AI-generated insight from background processing |
| `digest` | News handler | Visible | Daily news digest compiled from user interests/feeds |
| `reminder` | `remind_me` MCP tool | **Hidden** | Future-dated reminder (see below) |

## Grain and Consolidation

Moments are designed around a **time grain** — a date-based window (default last 24 hours, configurable via `lookback_days`) within which all user activity is collected and then consolidated into N moments (typically 1–3 per day).

The grain matters because a single moment should not represent a single session. Users interact across multiple sessions (chat, voice, uploads) throughout a day. An `session_chunk` summarizes **all activity** within its time window — multiple chat sessions, file uploads, and other interactions — giving a holistic view of what happened in that period.

### How consolidation works

1. **Hourly trigger** — The dreamer hourly cron job (`enqueue_dreaming_tasks`) finds users with new activity (messages or completed file uploads) since their last run.
2. **Phase 1 (first-order)** — `rem_build_moment()` runs against the user's 10 most recently updated sessions and creates `session_chunk` moments that consolidate conversation segments exceeding a token threshold (default 6000 tokens). These are deterministic, SQL-only summaries.
3. **Phase 2 (second-order)** — The dreaming agent reads all recent activity (moments, sessions, file uploads, referenced resources) within a date-based window (`NOW() - lookback_days`, default 24h) and produces 1–3 `dream` moments that synthesize cross-session themes and connections.
4. **Virtual "Today"** — `rem_moments_feed()` computes a `today` summary card on-the-fly from message counts, tokens, session counts, and moment counts for each active date. This is never stored — it's synthesized per query.

The result: for any given day, the feed shows a **Today card** (virtual summary), 0–N **activity chunks** (consolidated conversation segments), and 1–3 **dream moments** (AI-generated cross-session insights).

### Empty activity — exploration mode

The date window may contain **no activity** (the user didn't chat or upload anything). By default, dreaming is skipped when there's nothing to consolidate. However, when `allow_empty_activity_dreaming` is enabled, the dreamer enters **exploration mode**:

- Phase 1 produces nothing (no messages to consolidate)
- Phase 2 skips first-order consolidation and instead generates **random semantic searches** across the knowledge base — surfacing forgotten resources, old moments, and serendipitous connections
- The result is still 1–3 `dream` moments, but born from exploration rather than consolidation

This keeps the knowledge graph alive even during quiet periods, resurfacing older material that might be relevant.

> **Implementation note**: The Phase 1 query currently uses `ORDER BY updated_at DESC LIMIT 10` on sessions — this should migrate to a date-based window filter (`WHERE updated_at >= cutoff`) to match the grain model.

## Reminders

Reminders are "future moments" — created by the `remind_me` MCP tool with `moment_type='reminder'`.

A reminder has two important dates:

- **`created_at`** — when the user set the reminder ("I was thinking about this on Tuesday")
- **`starts_timestamp`** — when the reminder is due to fire ("it should go off Monday at 9am")

The daily summary badge counts by `created_at`. This is intentional: the badge shows "you set N reminders on this day", reflecting what the user was thinking about. The due date is a separate concern — when the reminder actually fires.

### How they work

1. User asks the agent to set a reminder (e.g. "remind me Monday at 9am to prep for standup")
2. Agent calls `remind_me` tool → creates:
   - A **pg_cron job** that fires a push notification at the scheduled time
   - A **reminder moment** with `starts_timestamp` = future fire date, `created_at` = now
3. The reminder moment has `graph_edges` with `relation="reminder"` linking back to the source session

### Feed integration

Reminders are "future moments" — they have `starts_timestamp` set to a future date. The feed excludes all future-dated moments by default (`starts_timestamp > NOW()`), which naturally hides reminders until they fire. Pass `include_future=true` to include them.

```bash
# Default: reminders hidden (starts_timestamp is in the future)
curl -s "$API/moments/feed?limit=3" -H "x-api-key: $P8_API_KEY" -H "x-user-id: $USER_ID"

# Include future-dated moments (reminders appear in feed)
curl -s "$API/moments/feed?limit=3&include_future=true" -H "x-api-key: $P8_API_KEY" -H "x-user-id: $USER_ID"
```

The daily summary still aggregates reminders regardless of the flag:

- `daily_reminder_counts` CTE counts reminder moments by `created_at` date
- Daily summary metadata includes `reminder_count`
- Flutter shows a bell badge on `DaySummaryCard` when `reminder_count > 0`
- Tapping the badge navigates to `RemindersScreen` → `GET /moments/reminders?created_on=YYYY-MM-DD`

### Moment schema

```json
{
  "name": "standup-prep",
  "moment_type": "reminder",
  "summary": "Prepare notes for the Monday standup meeting",
  "starts_timestamp": "2026-02-23T09:00:00+00:00",
  "created_at": "2026-02-21T20:21:26+00:00",
  "topic_tags": ["work", "standup"],
  "graph_edges": [
    {
      "target": "<session_id>",
      "relation": "reminder",
      "weight": 1.0,
      "reason": "Reminder 'standup-prep' created in this session"
    }
  ],
  "metadata": {
    "reminder_id": "ae042e5c-...",
    "job_name": "reminder-ae042e5c-...",
    "schedule": "0 9 * * 1",
    "recurrence": "recurring",
    "next_fire": "2026-02-23T09:00:00+00:00"
  }
}
```

### Tool context

The `remind_me` tool gets `user_id` and `session_id` from `ContextVars` — no need to pass them as parameters. The chat router sets them automatically before the agent runs, and the MCP middleware extracts `user_id` from the Bearer JWT.

### API

`GET /moments/reminders` — two independent date filters:

| Parameter | Filters on | Meaning |
|-----------|-----------|---------|
| `created_on` | `created_at` | Reminders set on this date. Used by the daily summary badge drill-down. |
| `due_on` | `starts_timestamp` | Reminders scheduled to fire on this date. Used to see "what's coming up today". |

Both can be combined. Omit both to get all reminders (limit 50).

#### curl examples

```bash
# What reminders did I set up on Friday?
curl -s "https://api.percolationlabs.ai/moments/reminders?created_on=2026-02-21" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
{
  "reminders": [
    {
      "id": "a9fc9425-b78a-5ac0-812c-c44f93ab8bdd",
      "name": "prep-standup-meeting",
      "summary": "Prep for standup meeting",
      "metadata": {
        "job_name": "reminder-f98338ce-...",
        "schedule": "0 9 22 2 *",
        "next_fire": "2026-02-22T09:00:00+00:00",
        "recurrence": "once",
        "reminder_id": "f98338ce-..."
      },
      "topic_tags": [],
      "starts_timestamp": "2026-02-22T09:00:00+00:00",
      "created_at": "2026-02-21T20:43:57.602470+00:00",
      "graph_edges": [
        {
          "target": "21c07c56-1799-4342-bfc9-ecb84612eb93",
          "relation": "reminder",
          "weight": 1.0,
          "reason": "Reminder 'prep-standup-meeting' created in this session"
        }
      ]
    }
  ],
  "count": 1
}
```

```bash
# What's due on Monday?
curl -s "https://api.percolationlabs.ai/moments/reminders?due_on=2026-02-23" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"

# Both filters combined
curl -s "https://api.percolationlabs.ai/moments/reminders?created_on=2026-02-21&due_on=2026-02-23" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"

# All reminders (limit 50)
curl -s "https://api.percolationlabs.ai/moments/reminders" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

### End-to-end flow

The full sequence from chat to badge:

```
1. User chats with agent: "remind me tomorrow at 9am to prep for standup"
2. Agent calls remind_me tool
3. remind_me creates:
   a. pg_cron job → POST /notifications/send at scheduled time
   b. moment_type='reminder' moment with:
      - starts_timestamp = 2026-02-22T09:00:00 (fire date)
      - created_at = now (creation date)
      - graph_edges = [{target: <session_id>, relation: "reminder"}]
      - source_session_id = <session_id>
4. Feed SQL (rem_moments_feed):
   - Future-dated moments excluded by default (starts_timestamp > NOW())
   - Reminders are naturally hidden because starts_timestamp is in the future
   - daily_reminder_counts CTE counts reminders by created_at
   - Daily summary metadata includes reminder_count: 1
   - Pass include_future=true to show future moments in the feed
5. Flutter DaySummaryCard shows bell badge when reminder_count > 0
6. Tapping badge → RemindersScreen → GET /moments/reminders?created_on=YYYY-MM-DD
```

#### Testing the full flow with curl

```bash
# Step 1: Chat with agent to create a reminder
SESSION_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
MSG_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
RUN_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')

curl -s "https://api.percolationlabs.ai/chat/$SESSION_ID" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID" \
  -H "x-agent-schema-name: general" \
  -d "{
    \"threadId\": \"$SESSION_ID\",
    \"runId\": \"$RUN_ID\",
    \"messages\": [{
      \"id\": \"$MSG_ID\",
      \"role\": \"user\",
      \"content\": \"Please remind me tomorrow at 9am to prep for standup\"
    }],
    \"tools\": [], \"context\": [], \"forwardedProps\": {}, \"state\": null
  }"

# Step 2: Verify reminder moment with graph_edges
curl -s "https://api.percolationlabs.ai/moments/reminders?created_on=$(date +%Y-%m-%d)" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
# → graph_edges[0].target should be the session_id from step 1

# Step 3: Verify feed shows reminder_count in daily summary
curl -s "https://api.percolationlabs.ai/moments/feed?limit=3" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
# → metadata.reminder_count should be > 0

# Step 4: Verify due_on filter
curl -s "https://api.percolationlabs.ai/moments/reminders?due_on=$(date -v+1d +%Y-%m-%d)" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
# → should return the same reminder (due tomorrow)
```

## Today — Virtual Daily Summary

The `today` moment is a virtual card generated by `rem_moments_feed()` for each date with activity. It is **never stored** — computed on the fly from messages, moments, and sessions. In the feed it appears as `event_type='daily_summary'` with `moment_type='daily_summary'`. 
> When a user interacts, we use a generated session id for this day with a deterministic hash — therefore the session id should be generated in advance on the moment even if no session is created yet. When the user starts chatting on a day, it always feels like a fresh session.

The Today card gives a quick snapshot of a day's activity: how many messages, tokens, sessions, and moments were created. It always sorts before real moments on the same date.
It also provides a location where users can create daily chat interactions on any topic with the fixed session id. Later these daily cards will appear in the feed and the user can continue to interact. Note compaction kicks in any long sessions.

Summary metadata includes:
- `message_count`, `total_tokens`, `session_count`, `moment_count`
- `reminder_count` — reminders **created** that day (by `created_at`, not due date)
- `sessions` — list of active sessions with names and agent names

Summary text adapts to recency:
- **Today**: `"Today: 12 messages across 2 session(s), 850 tokens. 2 moment(s)."`
- **Yesterday**: `"Yesterday: ..."`
- **Earlier**: `"Mon 15: ..."`

## Moment ↔ Session Architecture

Every moment has a 1:1 companion session. Sessions are 1:* with messages. This means every moment can be chatted about — the session stores name, description, and metadata for context injection so agents can answer questions like "what is this?" even without message history.

### `MemoryService.create_moment_session()` (`p8/services/memory.py`)

Unified entry point for creating a moment with its companion session:

```python
moment, session = await memory.create_moment_session(
    name="upload-q4-report",
    moment_type="content_upload",
    summary="Uploaded q4-report.txt (1 chunks, 78 chars).\nResources: q4-report-chunk-0000\n...",
    metadata={"file_name": "q4-report.txt", "resource_keys": ["q4-report-chunk-0000"], "source": "upload"},
    session_id=existing_session_id,  # optional — creates new if None
    user_id=user_id,
)
```

Three cases:
1. **No `session_id`** — creates a new session with `name=moment_name`, `mode=moment_type`, `metadata={...uploads...}`
2. **`session_id` exists** — merges upload info into `metadata.uploads[]`, accumulates `resource_keys[]`
3. **`session_id` doesn't exist yet** — creates session with that ID (supports deterministic IDs like Flutter's `todayChatId`)

### Upload path — what gets created

```
ingest("q4-report.txt", session_id="04468583-...") →
  ├── File:      name="q4-report", parsed_content="Revenue grew 23%..."
  ├── Resource:  name="q4-report-chunk-0000" (synced to kv_store for LOOKUP)
  ├── Moment:    name="upload-q4-report", type="content_upload"
  └── Session:   metadata.uploads[].file_name = "q4-report.txt"
                 metadata.resource_keys = ["q4-report-chunk-0000"]
```

**Moment data:**

```json
{
  "name": "upload-q4-report",
  "moment_type": "content_upload",
  "summary": "Uploaded q4-report.txt (1 chunks, 78 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23% YoY. Operating expenses down 8%...",
  "source_session_id": "04468583-...",
  "metadata": {
    "source": "upload",
    "file_id": "eca7dbdf-...",
    "file_name": "q4-report.txt",
    "chunk_count": 1,
    "resource_keys": ["q4-report-chunk-0000"]
  }
}
```

**Session data:**

```json
{
  "name": "Research",
  "mode": "chat",
  "description": "Uploaded q4-report.txt (1 chunks, 78 chars)...",
  "metadata": {
    "uploads": [
      {
        "source": "upload",
        "file_id": "eca7dbdf-...",
        "file_name": "q4-report.txt",
        "moment_id": "a321b811-...",
        "chunk_count": 1,
        "moment_name": "upload-q4-report",
        "moment_type": "content_upload",
        "resource_keys": ["q4-report-chunk-0000"]
      }
    ],
    "resource_keys": ["q4-report-chunk-0000"],
    "latest_summary": "Uploaded q4-report.txt (1 chunks, 78 chars)...",
    "latest_moment_id": "a321b811-..."
  }
}
```

**What the agent sees — moment injection** (`load_context()` → `format_moment_context()`):

```
[Session context]
Uploaded q4-report.txt (1 chunks, 78 chars).
Resources: q4-report-chunk-0000
Preview: Revenue grew 23% YoY. Operating expenses down 8%. Free cash flow reached 120M.
File: q4-report.txt
```

**What the agent sees — session instructions** (`ContextAttributes.render()`):

```
## Session Context
Session: Research
Context: {"uploads": [{"file_name": "q4-report.txt", "resource_keys": ["q4-report-chunk-0000"], ...}], "resource_keys": ["q4-report-chunk-0000"], "latest_summary": "Uploaded q4-report.txt...", "latest_moment_id": "a321b811-..."}
Use REM LOOKUP to retrieve full details for any keys listed above.
```

### Compaction path — what gets created

```
build_moment(session_id) →
  ├── Moment:  name="session-6d8bbc-20260222-chunk-0", type="session_chunk"
  │            summary = aggregated assistant messages (truncated 2000 chars)
  │            metadata = {message_count, token_count, chunk_index}
  └── Session: metadata += latest_moment_id, latest_summary, moment_count
```

**Moment data:**

```json
{
  "name": "session-6d8bbc-20260222-chunk-0",
  "moment_type": "session_chunk",
  "summary": "Discussed migration for service 1.\nDiscussed migration for service 3.\nDiscussed migration for service 5...",
  "source_session_id": "dcf3f701-...",
  "metadata": {
    "chunk_index": 0,
    "token_count": 1000,
    "message_count": 10
  },
  "previous_moment_keys": []
}
```

**Session data (after compaction):**

```json
{
  "name": "Sprint Planning",
  "metadata": {
    "moment_count": 1,
    "latest_summary": "Discussed migration for service 1.\nDiscussed migration for service 3...",
    "latest_moment_id": "50ddfbed-..."
  }
}
```

**What the agent sees — session instructions:**

```
## Session Context
Session: Sprint Planning
Context: {"moment_count": 1, "latest_summary": "Discussed migration for service 1.\nDiscussed migration for service 3...", "latest_moment_id": "50ddfbed-..."}
Use REM LOOKUP to retrieve full details for any keys listed above.
```

**Compaction breadcrumbs** (old assistant messages outside the recent window):

```
[Earlier: Discussed migration for service 1.… → REM LOOKUP session-6d8bbc-20260222-chunk-0]
```

### Feed LEFT JOIN — session data alongside moments

`rem_moments_feed` LEFT JOINs the companion session onto each moment. `session_name`, `session_description`, and `session_metadata` are NULL for daily summaries and standalone moments (LEFT JOIN graceful). The `GET /moments/` list and `GET /moments/{id}` endpoints also include these columns.

```bash
curl -s "$API/moments/feed?limit=2" -H "x-api-key: $P8_API_KEY" -H "x-user-id: $USER_ID"
```

```json
[
  {
    "event_type": "daily_summary",
    "name": "daily-2026-02-22",
    "summary": "Today: 12 messages across 2 session(s), 850 tokens...",
    "metadata": {
      "message_count": 12, "session_count": 2, "moment_count": 2, "reminder_count": 0,
      "sessions": [{"session_id": "c9dc6f35-...", "name": "Research", "agent_name": null}],
      "session_name": null, "session_metadata": null
    }
  },
  {
    "event_type": "moment",
    "name": "upload-q4-report",
    "moment_type": "content_upload",
    "summary": "Uploaded q4-report.txt (1 chunks, 78 chars)...",
    "session_id": "04468583-...",
    "metadata": {
      "moment_metadata": {"file_name": "q4-report.txt", "resource_keys": ["q4-report-chunk-0000"], "...": "..."},
      "session_name": "Research",
      "session_description": "Uploaded q4-report.txt (1 chunks, 78 chars)...",
      "session_metadata": {"uploads": ["..."], "resource_keys": ["q4-report-chunk-0000"], "...": "..."}
    }
  }
]
```

## Context Bootstrapping

When a user taps a moment card in the feed, the app opens a chat with that `session_id`. The agent needs enough context to have a meaningful conversation — moments provide this via injection and compaction.

Three mechanisms work together:

1. **Upload enrichment** — `ContentService` stores content previews and resource keys in the moment summary itself
2. **Moment injection** — `load_context()` prepends moments as system messages with metadata (resources, file name, topics)
3. **Compaction breadcrumbs** — old assistant messages become `[Earlier: <hint>… → REM LOOKUP <key>]`
4. **Session metadata** — `ContextAttributes.render()` injects `## Session Context` with session name and metadata JSON

The GeneralAgent system prompt has a `## Session Context` section instructing it to read these blocks, LOOKUP resource keys, follow breadcrumbs, and acknowledge what it already knows.

All examples below use `x-user-id` + `x-api-key` headers. Replace `$API` with your server URL (`http://localhost:8000` for local, `https://api.percolationlabs.ai` for prod).

### 1. Upload a file

```bash
curl -s -X POST "$API/content/" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID" \
  -F "file=@q4-report.txt"
```

```json
{
  "file": {
    "id": "a3f258c2-42be-5b72-94d2-233436519dde",
    "name": "q4-report",
    "uri": "s3://p8-percolate/.../q4-report.txt",
    "mime_type": "text/plain",
    "size_bytes": 326
  },
  "chunk_count": 1,
  "total_chars": 325,
  "resource_ids": ["a6bd3ffc-2aad-589e-a866-1f465c2d8c4d"],
  "session_id": "333062ab-f930-46d2-8e1e-37403d6d1ded"
}
```

### 2. Query the moment (with session data)

```bash
curl -s "$API/moments/?moment_type=content_upload&limit=1" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
[
  {
    "id": "8be13817-9866-59f0-8d89-0e3698d48001",
    "name": "upload-q4-report",
    "moment_type": "content_upload",
    "summary": "Uploaded q4-report.txt (1 chunks, 325 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23%...",
    "source_session_id": "333062ab-f930-46d2-8e1e-37403d6d1ded",
    "metadata": {
      "source": "upload",
      "file_id": "a3f258c2-...",
      "file_name": "q4-report.txt",
      "chunk_count": 1,
      "resource_keys": ["q4-report-chunk-0000"]
    },
    "session_name": "upload-q4-report",
    "session_description": "Uploaded q4-report.txt (1 chunks, 325 chars)...",
    "session_metadata": {
      "moment_id": "8be13817-...",
      "moment_name": "upload-q4-report",
      "moment_type": "content_upload",
      "file_name": "q4-report.txt",
      "resource_keys": ["q4-report-chunk-0000"],
      "source": "upload",
      "uploads": [{"moment_id": "8be13817-...", "file_name": "q4-report.txt", "...": "..."}]
    }
  }
]
```

### 3. LOOKUP a resource chunk

Resource chunks are synced to `kv_store` by a DB trigger, so the agent can retrieve full content:

```bash
curl -s "$API/query/" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $P8_API_KEY" \
  -d '{"mode": "LOOKUP", "query": "q4-report-chunk-0000"}'
```

```json
{
  "entity_key": "q4-report-chunk-0000",
  "entity_type": "resources",
  "content_summary": "Revenue grew 23% year-over-year driven by strong enterprise performance..."
}
```

### 4. Query the session timeline

When the app opens a chat for a moment card, it loads the session timeline — messages and moments interleaved chronologically:

```bash
SESSION_ID="333062ab-f930-46d2-8e1e-37403d6d1ded"
curl -s "$API/moments/session/$SESSION_ID" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
[
  {
    "event_type": "moment",
    "event_id": "8be13817-...",
    "event_timestamp": "2026-02-22T10:15:00+00:00",
    "name_or_type": "content_upload",
    "content_or_summary": "Uploaded q4-report.txt (1 chunks, 325 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23%...",
    "metadata": {
      "name": "upload-q4-report",
      "moment_metadata": {
        "source": "upload",
        "file_id": "a3f258c2-...",
        "file_name": "q4-report.txt",
        "resource_keys": ["q4-report-chunk-0000"]
      }
    }
  }
]
```

### 5. What the agent sees

When the app opens a session from a moment card, the agent receives context from two sources:

**A) Session metadata in system instructions** (`ContextAttributes.render()`):

```
## Session Context
Session: upload-q4-report
Context: {"moment_id": "8be13817-...", "file_name": "q4-report.txt", "resource_keys": ["q4-report-chunk-0000"], "uploads": [...]}
```

**B) Moment injection in message history** (`load_context()` → `format_moment_context()`):

```
[Session context]
Uploaded q4-report.txt (1 chunks, 325 chars).
Resources: q4-report-chunk-0000
Preview: Revenue grew 23% year-over-year...
File: q4-report.txt
```

**C) Compaction breadcrumbs** (old assistant messages outside the recent window):

```
[Earlier: Planned API v2 migration: endpoint inventory, OAuth2+PKCE auth, tok… → REM LOOKUP session-abc123-20260222-chunk-0]
```

### 6. After compaction — session metadata updated

```bash
# After build_moment() runs, the session metadata is enriched:
curl -s "$API/query/" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $P8_API_KEY" \
  -d '{"mode": "SQL", "query": "SELECT name, metadata FROM sessions WHERE id = '\''333062ab-...'\'' "}'
```

```json
{
  "name": "upload-q4-report",
  "metadata": {
    "uploads": [{"file_name": "q4-report.txt", "resource_keys": ["q4-report-chunk-0000"], "...": "..."}],
    "resource_keys": ["q4-report-chunk-0000"],
    "latest_moment_id": "d4e5f6a7-...",
    "latest_summary": "Revenue grew 23% year-over-year driven by strong enterprise performance...",
    "moment_count": 1
  }
}
```

## Integration Tests

```bash
uv run pytest tests/integration/content/test_upload_session_context.py -v
```

### Upload → Session → Agent Context (`test_upload_session_context.py`)

| # | Test | Proves |
|---|------|--------|
| 1 | `test_upload_creates_moment_with_metadata` | Moment has `file_name`, `resource_keys`, `source` in metadata |
| 2 | `test_upload_without_session_creates_session` | No `session_id` → new session with mode=`content_upload`, metadata with uploads[] |
| 3 | `test_upload_enriches_existing_session_metadata` | Pre-existing session gets upload info merged into metadata |
| 4 | `test_multiple_uploads_accumulate_metadata` | Two uploads → both in `metadata.uploads[]` and `resource_keys[]` |
| 5 | `test_agent_sees_upload_moment_in_context` | `load_context()` injects moment as system message with Resources/File lines |
| 6 | `test_agent_instructions_include_session_metadata` | `ContextAttributes.render()` includes `## Session Context` with metadata JSON |
| 7 | `test_end_to_end_upload_then_agent_context` | Full flow: create session → upload → chat → agent sees session metadata AND moment injection |
| 8 | `test_format_moment_context_includes_upload_fields` | `format_moment_context()` renders Resources, File, Topics correctly |
| 9 | `test_compaction_updates_session_metadata` | `build_moment()` stamps `latest_moment_id`, `latest_summary`, `moment_count` on session |
| 10 | `test_compaction_moment_injected_in_context` | After compaction, `load_context()` injects session_chunk moment as system message |
| 11 | `test_compaction_agent_instructions_include_session_metadata` | After compaction, `ContextAttributes.render()` includes `latest_summary` |
| 12 | `test_feed_returns_session_data_with_moments` | `rem_moments_feed` returns `session_name`, `session_metadata` via LEFT JOIN |
| 13 | `test_feed_handles_moments_without_session` | Standalone moments (no session) return NULL session fields (LEFT JOIN) |
| 14 | `test_compaction_breadcrumbs_in_old_messages` | Old assistant messages become `[Earlier: ... → REM LOOKUP <key>]` breadcrumbs |
| 15 | `test_resource_chunk_lookup_after_upload` | Resource chunks synced to `kv_store`, findable via LOOKUP |
| 16 | `test_audio_upload_creates_moment_and_session` | Audio → transcription → moment with `file_name`, session with `uploads[]` |
| 17 | `test_image_upload_creates_moment_with_image_uri` | Image → thumbnail → moment has `image_uri` (base64 data URI) |
| 18 | `test_upload_to_session_with_prior_compaction` | Upload to session that already has `session_chunk` moments — both coexist |
| 19 | `test_multiple_compaction_moments_chain` | Two `build_moment` calls → `m2.previous_moment_keys == [m1.name]`, `moment_count=2` |
| 20 | `test_upload_then_compaction_on_same_session` | Upload file then compact — session metadata has both `uploads[]` and `latest_moment_id` |

### Other test files

```bash
uv run pytest tests/integration/memory/test_moments.py -v       # moment building + session creation
uv run pytest tests/integration/memory/test_memory_pipeline.py -v  # upload→session, chaining
uv run pytest tests/integration/content/test_content.py -v       # ingest unit tests (mocked DB)
uv run pytest tests/integration/agents/test_remind_me.py -v      # reminder tool tests
```

Reminder tests:
- `test_remind_me_onetime_iso` — ISO datetime → one-time cron job + reminder moment
- `test_remind_me_recurring_cron` — cron expression → recurring job + moment
- `test_remind_me_missing_user` — no user context → error
- `test_remind_me_payload_in_job` — pg_cron job has correct payload for push notification
