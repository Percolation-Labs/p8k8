# Moments

A moment is the core data unit in the [Percolate RemApp](remapp-overview). Each moment represents a single memory — a meeting, note, voice memo, image, file, or observation — stored in the p8 `moments` table.

## Moment types

| Type | Icon | Description |
|------|------|-------------|
| `meeting` | calendar | Meetings with participants and action items |
| `note` | pencil | Text notes created by the user |
| `voice_note` | microphone | Recorded and transcribed audio |
| `image` | camera | Photos from camera or gallery |
| `content_upload` | paperclip | Uploaded files (xlsx, pptx, pdf, docx) |
| `observation` | chart | System-generated insights |
| `session_chunk` | chat | Consolidated conversation segments |
| `file` | paperclip | Generic file attachment |

## Thumbnail modes

Cards render differently based on `thumbnail_mode`:

- **image** — displays a network image URL
- **gradient** — local gradient background with a type icon overlay
- **icon** — file-extension icon (e.g., pdf, xlsx)
- **none** — text-only card

## Key fields

`id`, `name`, `moment_type`, `summary`, `image_uri`, `thumbnail_mode`, `start_time`, `topic_tags`, `emotion_tags`, `present_persons`, `source_session_id`

## Related

- [Feed Screen](remapp-feed) — where moments are displayed as cards
- [Chat](remapp-chat) — interactive drill-down into a moment
- [Content Upload](remapp-content-upload) — how new moments are created
