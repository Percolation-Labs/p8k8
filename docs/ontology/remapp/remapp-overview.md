# Percolate RemApp

A personal memory feed app built with Flutter and backed by the p8 platform. Think Instagram for your memories — a scrollable feed of moments you can chat with using the [REM query engine](overview).

## Core concepts

- **Moments** — every unit of memory: meetings, notes, voice memos, images, files, observations. Displayed as cards in a [Feed](remapp-feed).
- **Chat** — every moment is interactive. Tap a card to open a [Chat](remapp-chat) thread powered by REM.
- **Daily summaries** — virtual cards that aggregate a day's activity with reminders and schedule. See [Daily Summary](remapp-daily-summary).
- **Content upload** — users add new moments via text, voice, image, or file. See [Content Upload](remapp-content-upload).

## Tech stack

- **Frontend**: Flutter (Dart) — Android, iOS, web
- **Backend**: FastAPI + PostgreSQL + pgvector (p8 platform)
- **Streaming**: Server-Sent Events (SSE) via [AG-UI protocol](remapp-streaming)
- **Design**: Dark theme, warm accent (#C47B5A), glassmorphic cards

## Screens

| Screen | Purpose |
|--------|---------|
| [Feed](remapp-feed) | Infinite-scroll home screen |
| [Chat](remapp-chat) | Moment detail + conversation |
| [Content Upload](remapp-content-upload) | Add text, voice, image, file |
| [Integrations](remapp-integrations) | Google Drive, iCloud |

## Related

- [REM Query System](overview) — the query engine powering chat and search
- [Moments](remapp-moments) — data model and moment types
- [Streaming](remapp-streaming) — SSE event protocol
