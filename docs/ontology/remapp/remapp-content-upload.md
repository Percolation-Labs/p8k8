# Content Upload

The mechanism for adding new [Moments](remapp-moments) to the [Percolate RemApp](remapp-overview). Triggered by the [+] button on the [Feed](remapp-feed), which opens a bottom sheet with content type options.

## Content types

| Type | Action | Resulting moment_type |
|------|--------|-----------------------|
| Text Note | Opens text editor | `note` |
| Voice Note | Records audio, transcribes | `voice_note` |
| Image | Camera or gallery pick | `image` |
| File | File picker (pdf, xlsx, docx, pptx) | `content_upload` or `file` |
| Integration | Opens [Integrations](remapp-integrations) settings | — |

## How it works

1. User taps [+] on the feed screen
2. Bottom sheet presents the content type options
3. User selects a type and provides content (text, recording, photo, or file)
4. App calls `POST /content` with the upload payload
5. Backend creates a new moment and returns it
6. The feed refreshes to show the new moment card

## File handling

Uploaded files are stored and a moment is created with the appropriate `thumbnail_mode` — `icon` for document types (showing file-extension icon), `gradient` for audio, or `image` for photos with a preview URL.

## Related

- [Feed Screen](remapp-feed) — where new moments appear after upload
- [Moments](remapp-moments) — the data model for created content
- [Integrations](remapp-integrations) — cloud storage connections
