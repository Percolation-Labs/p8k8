# Daily Summary

A virtual feed item in the [Percolate RemApp](remapp-overview) that aggregates a day's activity into a single card. Always pinned as the first item on the [Feed](remapp-feed).

## What it contains

| Section | Description |
|---------|-------------|
| **Reminders** | Due items with status, due time, and swipe-to-complete |
| **Schedule** | Time-labeled entries linking to moments or reminders |
| **Chat messages** | Pre-populated conversation for the Today [Chat](remapp-chat) |

## How it works

The backend generates daily summary items via `GET /moments/feed`. Unlike regular [Moments](remapp-moments), a daily summary has `event_type: "daily_summary"` and does not correspond to a row in the moments table — it is synthesized from reminders, schedule data, and chat history for that day.

## Reminders

Each reminder has: `id`, `text`, `due_time`, `source_tool_call`, `status` (pending/completed), and `swiped_in` (boolean). Reminders can be created via the AI agent and managed from `GET /moments/reminders` and `DELETE /moments/reminders/{id}`.

## Today card behavior

- Pinned at the top of the feed, always visible
- Inline chat input visible without tapping — the default open conversation
- Shows today's reminders and upcoming schedule at a glance

## Related

- [Feed Screen](remapp-feed) — where the daily summary card appears
- [Chat](remapp-chat) — the inline chat experience
- [Moments](remapp-moments) — real moments vs. virtual daily summaries
