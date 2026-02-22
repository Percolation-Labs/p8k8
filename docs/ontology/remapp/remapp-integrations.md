# Integrations

Cloud storage connections in the [Percolate RemApp](remapp-overview). Allows syncing external files into the knowledge base as [Moments](remapp-moments).

## Supported providers

| Provider | Status | Description |
|----------|--------|-------------|
| Google Drive | Available | Connect a Google account + select sync folder |
| iCloud | Available | Connect an iCloud account + select sync folder |

## How it works

1. User navigates to the Integrations screen via [Content Upload](remapp-content-upload) or app settings
2. Each provider shows: toggle (on/off), connected account, sync folder
3. User taps "Connect" to authenticate with the provider
4. Once connected, files from the sync folder are ingested as moments

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/settings/integrations` | Current integration config |
| `PUT` | `/settings/integrations` | Update integration config |

## Related

- [Percolate RemApp](remapp-overview) — app overview
- [Content Upload](remapp-content-upload) — the other way to add content
- [Moments](remapp-moments) — what synced files become
