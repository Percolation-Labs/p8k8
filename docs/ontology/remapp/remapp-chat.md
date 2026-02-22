# Chat with Moments

Every moment in the [Percolate RemApp](remapp-overview) is interactive. Tapping a moment card on the [Feed](remapp-feed) opens a drill-down view with a chat thread powered by the [REM query engine](overview).

## How it works

1. The top section shows expanded moment detail — full summary, metadata, tags, media
2. Below is a chat thread specific to this moment
3. The user sends a message via `POST /chat/{chat_id}`
4. The backend streams a response token-by-token using [SSE](remapp-streaming)
5. The AI has full context of the moment when answering questions

## Chat capabilities

- Ask questions about the moment ("What were the action items?")
- Get summaries and highlights
- Explore related context from the knowledge base via REM queries
- Follow-up conversation with full thread history

## Today Chat

The [Daily Summary](remapp-daily-summary) card has a special inline chat input visible directly on the [Feed](remapp-feed) — users can start chatting without tapping into a detail view. This is the default open chat experience.

## Streaming protocol

Chat uses the AG-UI event protocol over SSE. See [Streaming](remapp-streaming) for the event sequence.

## Related

- [Percolate RemApp](remapp-overview) — app overview
- [Feed Screen](remapp-feed) — where moment cards live
- [Streaming](remapp-streaming) — SSE event protocol details
- [REM Query System](overview) — powers contextual answers
