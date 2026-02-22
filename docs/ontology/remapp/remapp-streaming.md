# Streaming Protocol

The [Percolate RemApp](remapp-overview) uses Server-Sent Events (SSE) for real-time token-by-token [Chat](remapp-chat) responses. The protocol follows the AG-UI event spec.

## Event sequence

A typical chat exchange produces this event stream:

```
RUN_STARTED        → { type, runId }
TEXT_MESSAGE_START  → { type, messageId, role }
TEXT_MESSAGE_CONTENT → { type, messageId, delta }  (repeated per token)
TEXT_MESSAGE_END    → { type, messageId }
RUN_FINISHED       → { type, runId }
```

## How it works

1. The Flutter app sends `POST /chat/{chat_id}` with the user message
2. The backend opens an SSE connection and streams events
3. `TEXT_MESSAGE_CONTENT` events carry incremental `delta` text tokens
4. The app renders tokens as they arrive for a responsive typing effect
5. `RUN_FINISHED` signals the response is complete

## Endpoint

`POST /chat/{chat_id}` — accepts a user message, returns an SSE stream. The `chat_id` maps to a session where the moment's context is loaded. The AI uses [REM queries](overview) to pull relevant knowledge before responding.

## Related

- [Chat](remapp-chat) — the user-facing chat experience
- [Percolate RemApp](remapp-overview) — app overview
- [REM Query System](overview) — powers contextual answers behind the stream
