# RemApp API

The backend endpoints serving the [Percolate RemApp](remapp-overview). Built on FastAPI with the p8 platform providing PostgreSQL + pgvector storage and [REM queries](overview).

## Core endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/moments/feed` | Cursor-paginated [Feed](remapp-feed) with daily summaries |
| `GET` | `/moments/today` | Virtual today moment |
| `GET` | `/moments/{id}` | Single [Moment](remapp-moments) with companion session |
| `GET` | `/moments/search` | Semantic search via embeddings with fuzzy fallback |
| `GET` | `/moments/reminders` | [Daily Summary](remapp-daily-summary) reminders |
| `DELETE` | `/moments/{id}` | Soft-delete a moment |
| `POST` | `/content` | [Content Upload](remapp-content-upload) — create moment from file |
| `POST` | `/chat/{chat_id}` | [Chat](remapp-chat) via AG-UI [Streaming](remapp-streaming) |
| `GET` | `/health` | Health check (tables, kv entries, embedding queue) |

## Pagination

The feed uses cursor-based pagination: `?before_date=ISO&limit=N`. Each response returns items older than the cursor date. The app tracks the oldest item's timestamp to request the next page on scroll.

## Search

`GET /moments/search?q=text&limit=N` embeds the query text, runs a vector similarity search, and falls back to fuzzy matching if no semantic results are found.

## Related

- [Percolate RemApp](remapp-overview) — app overview
- [Streaming](remapp-streaming) — SSE protocol for chat
- [REM Query System](overview) — underlying query engine
