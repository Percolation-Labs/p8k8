# Feed Screen

The home screen of the [Percolate RemApp](remapp-overview). An infinite-scroll feed of moment cards grouped by day, with the [Daily Summary](remapp-daily-summary) always pinned at top.

## How it works

1. On load, the app calls `GET /moments/feed?limit=N` which returns cursor-paginated feed items
2. Items are grouped by date with separators: Today, Yesterday, Feb 15, etc.
3. Scrolling down triggers pagination via `before_date` cursor — loads older moments
4. The feed refreshes automatically on app resume and on a timer

## Feed items

The feed mixes two event types:

| Type | Description |
|------|-------------|
| `daily_summary` | Virtual card with reminders, schedule, and chat. See [Daily Summary](remapp-daily-summary) |
| `moment` | A real [Moment](remapp-moments) card — meeting, note, voice memo, etc. |

## Card layout

Each moment card shows: type icon, title, people/entities, relative time ("2h ago"), and a summary preview. Cards with images display a network thumbnail. Cards without images use a gradient or icon based on [thumbnail mode](remapp-moments).

## Key behaviors

- **Pull-to-refresh** reloads the feed from the server
- **Tap a card** opens the [Chat](remapp-chat) drill-down
- **[+] button** opens the [Content Upload](remapp-content-upload) sheet

## Related

- [Percolate RemApp](remapp-overview) — app overview
- [Moments](remapp-moments) — data model behind feed cards
- [Daily Summary](remapp-daily-summary) — the pinned Today card
