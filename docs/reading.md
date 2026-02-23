# Reading

Track which news items users click or bookmark, aggregate them into a daily reading moment, summarize with a cheap model, and provide a horizontal-swipe drill-down with chat.

## Motivation

The news/digest system ingests articles and surfaces them in the feed, but there's no signal for what the user actually engages with. Interaction tracking (clicks and bookmarks) closes this loop — it tells the system which topics the user cares about (not just what was served), enables lightweight summarization of consumed content, and creates a daily mosaic card that reflects actual reading behavior.

## Moment Type

`moment_type = "reading"` — one per user per day, upserted on each interaction (click or bookmark).

Unlike digests (which are pushed), reading moments are pulled — they only exist when the user interacts with something. A day with no clicks or bookmarks produces no reading moment.

## Interaction Tracking

Three actions are supported: `click`, `bookmark`, and `unbookmark`.

- **click** and **bookmark** both upsert the daily reading moment and add the resource as an item.
- **unbookmark** only removes the user from the resource's `bookmarked_by` list — it does not modify the reading moment.

### Endpoint

```
POST /resources/{resource_id}/reading
```

Request:

```json
{
  "action": "click"
}
```

Actions: `click`, `bookmark`, `unbookmark`.

Response (click/bookmark):

```json
{
  "moment_id": "8be13817-...",
  "action": "click",
  "duplicate": false,
  "item_count": 3
}
```

The endpoint is fire-and-forget from the client's perspective — the user taps a news link, the app opens the URL and records the interaction in parallel. No blocking.

### What happens on the backend

1. Resolve the resource by ID — pull `name`, `uri`, `image_uri`, `tags` from the `resources` table
2. For `bookmark`, persist the user in `resource.metadata.bookmarked_by[]`
3. For `unbookmark`, remove the user from `bookmarked_by` and return early (no reading moment update)
4. Upsert the daily reading moment for `(user_id, date)`:
   - **First interaction of the day** — create a new moment with `moment_type='reading'`, deterministic name `reading-{date}`, and a companion session
   - **Subsequent interactions** — append to `metadata.items[]` and update counts (deduplicated by `resource_id`)
5. Each item records its `action` type (`click` or `bookmark`) and `timestamp`
6. Add a `graph_edge` from the reading moment to the resource (`relation: "read"`)
7. Regenerate the mosaic thumbnail from all items (fire-and-forget)

### Deterministic moment identity

Use a deterministic name: `reading-YYYY-MM-DD` (e.g. `reading-2026-02-22`). Lookup by `(user_id, name)` to find or create. This avoids duplicates and makes the moment addressable.

The companion session uses the same pattern: `reading-YYYY-MM-DD` with `mode='reading'`.

## Moment Schema

```json
{
  "name": "reading-2026-02-22",
  "moment_type": "reading",
  "summary": null,
  "topic_tags": ["python", "ai", "ecology"],
  "metadata": {
    "source": "reading_tracker",
    "resource_count": 3,
    "items": [
      {
        "resource_id": "a6bd3ffc-...",
        "uri": "https://example.com/article-1",
        "title": "New Python 3.15 Features",
        "image_uri": "https://example.com/thumb.jpg",
        "tags": ["python", "release"],
        "action": "click",
        "timestamp": "2026-02-22T14:30:00Z"
      },
      {
        "resource_id": "c3e1a9b2-...",
        "uri": "https://example.com/article-2",
        "title": "Forest Restoration Progress",
        "image_uri": "https://example.com/forest.jpg",
        "tags": ["ecology", "restoration"],
        "action": "bookmark",
        "timestamp": "2026-02-22T16:45:00Z"
      },
      {
        "resource_id": "d4f2b8c3-...",
        "uri": "https://example.com/article-3",
        "title": "Trail Running Technique",
        "image_uri": "https://example.com/trail.jpg",
        "tags": ["running", "outdoors"],
        "action": "click",
        "timestamp": "2026-02-22T18:10:00Z"
      }
    ]
  },
  "graph_edges": [
    {
      "target": "new-python-315-features",
      "relation": "read",
      "weight": 1.0
    },
    {
      "target": "forest-restoration-progress",
      "relation": "read",
      "weight": 1.0
    },
    {
      "target": "trail-running-technique",
      "relation": "read",
      "weight": 1.0
    }
  ]
}
```

The `summary` field starts null and is populated by the nano summarizer (see below).

## Nano Summarization

A background worker summarizes the day's reading using a cheap model. This is quota-gated — it costs tokens, so free-tier users get fewer summaries.

### Task type

`task_type = "reading_summarize"`, tier = `micro`.

### Trigger

Two paths:

1. **End-of-day cron** — `enqueue_reading_tasks()` runs daily (e.g. `0 6 * * *` UTC). Finds users with reading moments that have `summary IS NULL` and at least 1 item. Enqueues a `reading_summarize` task.
2. **Threshold trigger** — when a reading moment accumulates N items (e.g. 5), enqueue immediately. This gives active readers a summary before the day ends.

### Handler

`ReadingSummarizerHandler` in `p8/workers/handlers/reading.py`:

1. Load the reading moment for `(user_id, date)`
2. For each item (click or bookmark), build a line with title, URI, tags, and action type
3. Build prompt: "Summarize what the user read today. Articles: {list}"
4. Call nano model (`openai:gpt-4.1-nano` or `anthropic:claude-haiku`) — cheapest available
5. Update `moment.summary` with the result
6. Record usage: `increment_quota('reading_summarize_io_tokens', io_tokens)`

### Prompt

```
You are summarizing a user's daily reading. They interacted with these articles:

{for each item}
- "{title}" ({uri}) [{tags}] ({action})
{end}

Write a 2-3 sentence summary of what they read today. Focus on themes and what's interesting, not just listing titles. Write in second person ("You read about...").
```

### Quota

New resource type: `reading_summarize_io_tokens`.

| Plan | Monthly Limit |
|------|---------------|
| free | 5,000 |
| pro | 25,000 |
| team | 50,000 |
| enterprise | 200,000 |

Pre-flight check before running the handler, same pattern as dreaming.

## Feed Integration

### Daily summary badge

`rem_moments_feed()` already computes `resource_counts` per day. Add a `reading_count` to the daily summary metadata — count of reading moments for that date (0 or 1). Flutter shows a reading badge on `DaySummaryCard` when `reading_count > 0`.

### Reading card in feed

Reading moments appear as regular moments in the feed (`event_type='moment'`, `moment_type='reading'`). The Flutter app renders them with a dedicated `ReadingCard` widget instead of the generic `MomentCard`.

The card shows:
- **Header**: "Reading" label + date
- **Body**: Mosaic grid of thumbnails from `metadata.items[].image_uri` (2x2 or 3-column grid depending on count)
- **Footer**: Summary text (if available) + item count badge
- **Tap action**: Navigate to `ReadingDetailScreen`

## Drill-Down: ReadingDetailScreen

When the user taps a reading card, the app opens a detail screen with two zones:

### Top: Horizontal card carousel

A `PageView` of items, ordered by `timestamp`. Each page shows:
- Article thumbnail (full width)
- Title overlay
- Tags as chips
- "Open in browser" button
- Swipe left/right to navigate

### Bottom: Chat area

Same SSE chat integration as `MomentDetailScreen`. The session ID comes from the reading moment's companion session (`source_session_id`).

The agent receives context via the standard moment injection path:
- Session metadata includes `items[]` with titles, URIs, and action types
- Moment summary (if nano model has run) is injected
- Agent can answer "what did I read about?" or "tell me more about the forest article"

### Agent context

When the user opens the reading detail screen, the agent sees:

```
## Session Context
Session: reading-2026-02-22
Context: {"source": "reading_tracker", "resource_count": 5, "items": [...]}
Use REM LOOKUP to retrieve full details for any keys listed above.
```

And via moment injection:

```
[Session context]
Daily reading: 5 articles (3 clicked, 2 bookmarked).
Summary: You read about new Python 3.15 features and forest restoration efforts...
Topics: python, ai, ecology
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/resources/{resource_id}/reading` | Record a click/bookmark/unbookmark — upsert daily reading moment |
| `GET` | `/resources/reading` | Fetch reading moment for a date (YYYY-MM-DD, defaults to today) |

The drill-down data comes from the moment itself (`metadata.items[]`) — no separate endpoint needed. The chat uses the existing `/chat/{session_id}` endpoint.

## Flutter Widgets

| Widget | Location | Purpose |
|--------|----------|---------|
| `ReadingCard` | `widgets/reading_card.dart` | Mosaic grid card for the feed |
| `ReadingDetailScreen` | `screens/reading_detail_screen.dart` | Carousel + chat drill-down |

### Interaction recording in resources_screen.dart

In the existing `_openUri()` method (or equivalent tap handler), add an API call before/after launching the URL:

```dart
// Fire-and-forget click recording
api.recordReading(resource.id, action: 'click').catchError((_) {});
// Then open the URL as before
launchUrl(Uri.parse(resource.uri));
```

For bookmarks, call the same endpoint with the `bookmark` or `unbookmark` action:

```dart
api.recordReading(resource.id, action: isBookmarked ? 'unbookmark' : 'bookmark');
```

## Worker Registration

Register `ReadingSummarizerHandler` in `p8/workers/handlers/__init__.py`:

```python
_HANDLER_REGISTRY = {
    "file_processing": FileProcessingHandler,
    "dreaming": DreamingHandler,
    "news": NewsHandler,
    "reading_summarize": ReadingSummarizerHandler,  # new
}
```

Add `enqueue_reading_tasks()` to `sql/03_qms.sql` with a daily pg_cron schedule.

## Queue SQL

```sql
-- Enqueue reading summarization for users with unsummarized reading moments (clicks + bookmarks)
CREATE OR REPLACE FUNCTION enqueue_reading_tasks()
RETURNS void AS $$
INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload)
SELECT DISTINCT
    'reading_summarize',
    'micro',
    m.user_id,
    m.tenant_id,
    jsonb_build_object(
        'trigger', 'scheduled',
        'reading_moment_id', m.id,
        'date', m.name,
        'enqueued_at', NOW()
    )
FROM moments m
WHERE m.moment_type = 'reading'
  AND m.summary IS NULL
  AND (m.metadata->>'resource_count')::int >= 1
  AND NOT EXISTS (
      SELECT 1 FROM task_queue tq
      WHERE tq.user_id = m.user_id
        AND tq.task_type = 'reading_summarize'
        AND tq.status IN ('pending', 'processing')
  );
$$ LANGUAGE sql;

-- Daily at 6am UTC
SELECT cron.schedule('enqueue-reading-tasks', '0 6 * * *',
    $$SELECT enqueue_reading_tasks()$$);
```

## Interaction with Dreaming

Reading moments feed into the dreaming system naturally:
- Phase 2 of dreaming already loads recent moments — reading moments are included
- The dreaming agent can discover patterns like "you've been reading a lot about Python lately"
- `graph_edges` with `relation: "read"` give the dreaming agent explicit links to follow

No special integration needed — the existing moment/graph_edge infrastructure handles it.

## Interaction with News

Reading data creates a feedback loop for the news handler:
- Future: weight feed categories by click frequency (users who click ecology articles get more ecology)
- Future: skip articles similar to ones the user never clicks
- For now: reading and news are independent — news pushes, reading tracks

## Key Files

| File | Role |
|------|------|
| `p8/api/routers/resources.py` | `POST /{resource_id}/reading` and `GET /reading` endpoints |
| `p8/workers/handlers/reading.py` | `ReadingSummarizerHandler` — nano model summarization |
| `p8/workers/handlers/__init__.py` | Register new handler in `_HANDLER_REGISTRY` |
| `p8/services/queue.py` | Threshold-based enqueue (optional, on Nth click) |
| `p8/services/usage.py` | Add `reading_summarize_io_tokens` quota |
| `sql/03_qms.sql` | `enqueue_reading_tasks()` + pg_cron schedule |
| `sql/02_install.sql` | Update `rem_moments_feed()` to include `reading_count` in daily summary |

## Implementation Order

1. **Reading endpoint** — `POST /resources/{resource_id}/reading` with upsert logic (click, bookmark, unbookmark)
2. **Flutter interaction recording** — fire-and-forget calls for clicks and bookmarks
3. **Feed integration** — `reading_count` in daily summary, `ReadingCard` widget
4. **Drill-down screen** — `ReadingDetailScreen` with carousel + chat
5. **Nano summarizer** — worker handler + queue + quota
6. **Cron job** — `enqueue_reading_tasks()` daily schedule
