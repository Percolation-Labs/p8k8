# Related Searches ("People also search for")

> Spec for issue #14 — expose Google's "People also search for" signal as a
> service + agent tool.

## Goal

Given a query, return a short list of related phrases — the same category of
signal as the "People also search for" / "Related searches" widgets Google
shows on its results page.

## Options considered

### 1. Scrape the Google SERP HTML directly

The literal reading of the issue title. Fetch
`https://www.google.com/search?q=...` and parse the "People also search for"
widget out of the HTML.

Rejected for the simple version:

- Google obfuscates and rotates the widget's CSS class names/DOM structure
  frequently, so a scraper needs ongoing maintenance.
- Needs an HTML parser (`beautifulsoup4` / `selectolax`) — not currently a
  dependency of this repo.
- Needs UA rotation, rate limiting, and CAPTCHA handling to be reliable at any
  volume — Google actively pushes back on scripted SERP access, which is a
  ToS gray area.
- No JSON contract — output shape depends on whatever HTML ships that day.

### 2. Google's public autosuggest endpoint (chosen for the simple version)

`https://suggestqueries.google.com/complete/search?client=firefox&q=...`

This is the same endpoint that powers Google's search-box autocomplete. It
returns query completions/related phrases as JSON, keyed off the same
underlying "what do people search for around this term" signal — not
pixel-identical to the SERP widget, but the same category of data, with:

- No HTML parsing — plain JSON.
- No API key.
- No new dependency — `httpx` is already used by `p8/services/web_search.py`.
- Stable, long-standing endpoint (also used by many other open-source tools
  for the same purpose).

Trade-off: it returns *autocomplete-style* completions rather than the exact
"People also search for" chips Google shows on a rendered results page. For
the "simple version" ask this is an acceptable proxy signal.

## Simple version (this PR)

Mirrors the layering of the existing `web_search` tool:

```
p8/api/tools/related_searches.py   MCP/agent tool — error handling, {status, ...} shape
        │
        ▼
p8/services/related_searches.py    async related_searches(query, max_results) -> list[str]
        │
        ▼
suggestqueries.google.com/complete/search   (httpx GET, JSON)
```

- `p8/services/related_searches.py::related_searches(query, max_results=8)` —
  calls the autosuggest endpoint, dedupes (case-insensitive) and drops the
  original query if Google echoes it back, caps at `max_results`.
- `p8/api/tools/related_searches.py::related_searches(query, max_results=8)` —
  MCP/agent tool wrapper. Catches `httpx` errors and returns
  `{"status": "error", ...}` instead of raising, matching `web_search`'s
  error contract.
- Registered as an MCP tool (`p8/api/mcp_server.py`) and in the direct-call
  `TOOL_REGISTRY` (`p8/api/tools/__init__.py`).

No API key, no new dependency, no quota check, no `Resource`/`Moment`
persistence — kept intentionally minimal per the "simple version" ask.

## Future work (not in this PR)

- **Direct SERP scraping (option 1)** if the autosuggest proxy signal proves
  too different from the real "People also search for" widget for some use
  case — would need `beautifulsoup4`/`selectolax`, UA rotation, and retry/
  backoff.
- **Caching** — related phrases for a given query change slowly; a
  `kv_store` TTL cache would cut latency and outbound request volume.
- **Quota + persistence** — add a `related_searches_daily` usage resource
  (see `p8/services/usage.py`) and optionally save results as `Resource`
  entities / a `Moment`, the way `web_search` does, if this becomes a
  user-facing, budget-relevant tool rather than an internal utility.
- **REST endpoint** — expose via `p8/api/routers/` if external (non-agent)
  callers need it.
