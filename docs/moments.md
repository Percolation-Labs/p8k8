# Moments

Moments are the core content units in the feed. Each moment has a `moment_type` that determines how it's displayed and aggregated.

## Moment Types

| Type | Source | Feed Visibility | Description |
|------|--------|-----------------|-------------|
| `session_chunk` | Dreaming/compaction | Visible | Summarized chunk of a chat session |
| `content_upload` | File upload pipeline | Visible | Uploaded file (PDF, image, voice, etc.) |
| `notification` | `/notifications/send` | Visible | Push notification that was delivered |
| `dream` | Dreaming CronJob | Visible | AI-generated insight from background processing |
| `reminder` | `remind_me` MCP tool | **Hidden** | Future-dated reminder (see below) |

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

## Daily Summary (Virtual Moments)

The `rem_moments_feed` SQL function generates virtual `daily_summary` rows for each date with activity. These are not stored — they're computed on the fly from messages, moments, and sessions.

Summary metadata includes:
- `message_count`, `total_tokens`, `session_count`, `moment_count`
- `reminder_count` — reminders **created** that day (by `created_at`, not due date)
- `sessions` — list of active sessions with names and agent names

## Context Bootstrapping

When a user taps a moment card in the feed, the app opens a chat with that `session_id`. The agent needs enough context to have a meaningful conversation — moments provide this via injection and compaction.

Three mechanisms work together:

1. **Upload enrichment** — `ContentService` stores content previews and resource keys in the moment summary itself
2. **Moment injection** — `load_context()` prepends moments as system messages with metadata (resources, file name, topics)
3. **Compaction breadcrumbs** — old assistant messages become `[Earlier: <hint>… → REM LOOKUP <key>]`

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
    "uri": "s3://p8-percolate/dddddddd-0000-0000-0000-000000000001/2026/02/21/q4-report.txt",
    "mime_type": "text/plain",
    "size_bytes": 326,
    "parsed_content": "Revenue grew 23% year-over-year driven by strong enterprise performance..."
  },
  "chunk_count": 1,
  "total_chars": 325,
  "resource_ids": ["a6bd3ffc-2aad-589e-a866-1f465c2d8c4d"],
  "session_id": "333062ab-f930-46d2-8e1e-37403d6d1ded"
}
```

### 2. Query the upload moment

The upload created a `content_upload` moment with an enriched summary — content preview, resource keys, and file stats baked in:

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
    "summary": "Uploaded q4-report.txt (1 chunks, 325 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23% year-over-year driven by strong enterprise performance. Operating expenses decreased 8% due to headcount optimization. Free cash flow reached 120M, up from 89M. The board approved a 5…",
    "source_session_id": "333062ab-f930-46d2-8e1e-37403d6d1ded",
    "metadata": {
      "source": "upload",
      "file_id": "a3f258c2-42be-5b72-94d2-233436519dde",
      "file_name": "q4-report.txt",
      "chunk_count": 1,
      "resource_keys": ["q4-report-chunk-0000"]
    }
  }
]
```

The agent sees three things it can act on: the filename, the chunk key to LOOKUP, and enough preview text to start a conversation without asking "what file?".

### 3. Check the session data

The upload created a session with metadata containing the resource keys and source filename. This metadata is injected into the model's instructions as a `Context:` block, so the agent knows which resources to LOOKUP.

```bash
SESSION_ID="333062ab-f930-46d2-8e1e-37403d6d1ded"  # from step 1

curl -s "$API/query/" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $P8_API_KEY" \
  -d "{
    \"mode\": \"SQL\",
    \"query\": \"SELECT id, name, metadata FROM sessions WHERE id = '$SESSION_ID'\"
  }"
```

```json
[
  {
    "id": "333062ab-f930-46d2-8e1e-37403d6d1ded",
    "name": "upload: q4-report.txt",
    "metadata": {
      "source": "q4-report.txt",
      "moment_id": "8be13817-9866-59f0-8d89-0e3698d48001",
      "resource_keys": ["q4-report-chunk-0000"]
    }
  }
]
```

When a user opens this session in chat, `ChatController.prepare()` passes `session.metadata` to the `ContextInjector`, which renders it into the agent's system instructions:

```
## Session Context
Session: upload: q4-report.txt
Context: {"source": "q4-report.txt", "moment_id": "8be13817-...", "resource_keys": ["q4-report-chunk-0000"]}
Use REM LOOKUP to retrieve full details for any keys listed above.
```

The agent sees the resource keys and can call `search("LOOKUP q4-report-chunk-0000")` to load the full chunk content.

### 4. Query the session timeline

When the app opens a chat for a moment card, it loads the session timeline — messages and moments interleaved chronologically:

```bash
curl -s "$API/moments/session/$SESSION_ID" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
[
  {
    "event_type": "moment",
    "event_id": "8be13817-9866-59f0-8d89-0e3698d48001",
    "event_timestamp": "2026-02-21T20:43:45.817098+00:00",
    "name_or_type": "content_upload",
    "content_or_summary": "Uploaded q4-report.txt (1 chunks, 325 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23% year-over-year...",
    "metadata": {
      "name": "upload-q4-report",
      "moment_metadata": {
        "source": "upload",
        "file_id": "a3f258c2-42be-5b72-94d2-233436519dde",
        "file_name": "q4-report.txt",
        "chunk_count": 1,
        "resource_keys": ["q4-report-chunk-0000"]
      }
    }
  }
]
```

### 5. Query the feed

The feed interleaves a virtual `daily_summary` card with real moments. The daily summary has session names the agent can use as conversation starters:

```bash
curl -s "$API/moments/feed?limit=3" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
[
  {
    "event_type": "daily_summary",
    "event_date": "2026-02-21",
    "name": "daily-2026-02-21",
    "summary": "Today: 47 messages across 5 session(s), 1761 tokens. 5 moment(s).",
    "session_id": "c9dc6f35-f654-5447-8af7-901c06ab328f",
    "metadata": {
      "sessions": [
        {"name": "dreaming-dddddddd-...", "agent_name": "dreaming-agent", "session_id": "9f6fbea2-..."},
        {"name": "test-dream-session-arch", "agent_name": null, "session_id": "70454804-..."},
        {"name": "test-dream-session-ml", "agent_name": null, "session_id": "82b14425-..."}
      ],
      "moment_count": 5,
      "total_tokens": 1761,
      "message_count": 47,
      "session_count": 5,
      "reminder_count": 0
    }
  },
  {
    "event_type": "moment",
    "name": "dream-ml-pipeline-optimization",
    "moment_type": "dream",
    "summary": "Our ML pipeline currently handles 10M records daily with key bottlenecks in feature computation...",
    "metadata": {
      "topic_tags": ["machine-learning", "data-pipeline", "feature-engineering"]
    }
  },
  {
    "event_type": "moment",
    "name": "upload-q4-report",
    "moment_type": "content_upload",
    "summary": "Uploaded q4-report.txt (1 chunks, 325 chars).\nResources: q4-report-chunk-0000\nPreview: Revenue grew 23%..."
  }
]
```

### 6. Query today summary

```bash
curl -s "$API/moments/today" \
  -H "x-api-key: $P8_API_KEY" \
  -H "x-user-id: $USER_ID"
```

```json
{
  "name": "today",
  "moment_type": "today_summary",
  "summary": "Today: 47 messages across 5 session(s), 1761 tokens. 5 moment(s).",
  "metadata": {
    "message_count": 47,
    "total_tokens": 1761,
    "moment_count": 5,
    "sessions": [
      {"name": "dreaming-dddddddd-...", "agent_name": "dreaming-agent", "session_id": "9f6fbea2-..."},
      {"name": "test-dream-session-arch", "agent_name": null, "session_id": "70454804-..."},
      {"name": "test-dream-session-ml", "agent_name": null, "session_id": "82b14425-..."}
    ]
  }
}
```

### How the agent uses this

When the app opens a session from a moment card, `load_context()` injects the moment as a system message. For the upload above, the agent receives:

```
[Session context]
Uploaded q4-report.txt (1 chunks, 325 chars).
Resources: q4-report-chunk-0000
Preview: Revenue grew 23% year-over-year driven by strong enterprise performance...
File: q4-report.txt
```

The agent can then:
- Acknowledge the file without asking "what file?"
- Call `search("LOOKUP q4-report-chunk-0000")` to load the full content
- Use the preview to start a conversation: "I see the Q4 report — revenue was up 23%. Want me to dig into the details?"

For older sessions with compaction, assistant messages outside the recent window become breadcrumbs:

```
[Earlier: Planned API v2 migration: endpoint inventory (/users, /sessions, /moments), OAuth2+PKCE auth, tok… → REM LOOKUP session-abc-chunk-0]
```

The agent can follow the LOOKUP to retrieve the full moment when the user asks about earlier topics.

### Integration tests

```bash
uv run pytest tests/integration/memory/test_moments.py -v
uv run pytest tests/integration/agents/test_remind_me.py -v
```

Moment tests:
- `test_upload_moment_has_content_preview` — upload → summary contains `Preview:` and `Resources:`
- `test_moment_injection_includes_metadata` — `load_context()` → system message includes `Resources:`, `File:` lines
- `test_compacted_messages_include_summary` — compacted breadcrumbs contain summary hint + LOOKUP key
- `test_today_summary_includes_session_names` — today summary metadata includes session names

Reminder tests:
- `test_remind_me_onetime_iso` — ISO datetime → one-time cron job + reminder moment
- `test_remind_me_recurring_cron` — cron expression → recurring job + moment
- `test_remind_me_missing_user` — no user context → error
- `test_remind_me_payload_in_job` — pg_cron job has correct payload for push notification
