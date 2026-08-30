"""Ingestion clients for the Percolate detection core's v1 source set (§15.1).

Five zero-auth, free sources chosen for domain diversity per the spec's own
v1 build-scope note: SEC EDGAR (corporate/finance), arXiv (research), HN
(dev/tech subculture), Federal Register (US regulatory), Wikipedia
RecentChanges (general world-event proxy).

Every fetch function returns a list of normalized raw items:

    {
        "title": str,
        "summary": str,
        "url": str,                    # primary source link (§16.5)
        "published_at": datetime | None,
        "source_key": str,
        "entity_names": list[str],     # candidate entity names, pulled directly
                                        # from source-native fields (filer name,
                                        # agency name, author, page title, URL
                                        # domain) rather than NLP extraction —
                                        # every v1 source already carries clean
                                        # entity-shaped fields.
    }

No entity resolution or scoring happens here — that's entity_resolution.py
and corroboration.py. This module only talks to the network and normalizes.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 15.0
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_CIK_RE = re.compile(r"^(?P<name>.*?)\s*(?:\([A-Z0-9.\-]{1,10}\)\s*)?\(CIK (?P<cik>\d+)\)\s*$")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_sec_edgar(
    *, query: str = "acquisition", forms: str = "8-K", limit: int = 20, user_agent: str,
) -> list[dict]:
    """SEC EDGAR full-text search (§15.1) — needs a User-Agent header, no other auth.

    ``query`` is a full-text search term; EDGAR's FTS endpoint requires one.
    Defaults to a broad, high-signal term so a first run without a caller-supplied
    query still returns real current-week filings.
    """
    url = "https://efts.sec.gov/LATEST/search-index"
    params = {"q": f'"{query}"', "forms": forms}
    headers = {"User-Agent": user_agent}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = []
    for hit in data.get("hits", {}).get("hits", [])[:limit]:
        src = hit.get("_source", {})
        display_names = src.get("display_names") or []
        entity_names = []
        for raw in display_names:
            m = _CIK_RE.match(raw or "")
            entity_names.append(m.group("name").strip() if m else raw.strip())
        entity_names = [n for n in entity_names if n]

        cik = (src.get("ciks") or [None])[0]
        adsh = src.get("adsh")
        accession_nodash = (adsh or "").replace("-", "")
        cik_nolead = str(int(cik)) if cik and cik.isdigit() else cik
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_nolead}/{accession_nodash}/{adsh}-index.htm"
            if cik and adsh else "https://www.sec.gov/edgar/search/"
        )

        form = src.get("form") or ", ".join(src.get("root_forms") or [])
        title = f"{entity_names[0] if entity_names else 'Unknown filer'} — {form} filing"
        items.append({
            "title": title,
            "summary": f"{form} filing by {', '.join(entity_names) or 'unknown filer'} "
                       f"(items: {', '.join(src.get('items') or [])})".strip(),
            "url": doc_url,
            "published_at": _parse_dt(src.get("file_date")),
            "source_key": "sec_edgar",
            "entity_names": entity_names,
        })
    return items


async def fetch_arxiv(
    *, query: str = "cat:cs.AI", limit: int = 20,
) -> list[dict]:
    """arXiv API (§15.1) — must use https, not http; no auth."""
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(limit),
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.text

    root = ElementTree.fromstring(body)
    items: list[dict] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title_el = entry.find("atom:title", _ATOM_NS)
        summary_el = entry.find("atom:summary", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        link_el = entry.find("atom:link[@rel='alternate']", _ATOM_NS)
        authors = [
            (a.find("atom:name", _ATOM_NS).text or "").strip()
            for a in entry.findall("atom:author", _ATOM_NS)
            if a.find("atom:name", _ATOM_NS) is not None
        ]
        title = " ".join((title_el.text or "").split()) if title_el is not None else "Untitled"
        summary = " ".join((summary_el.text or "").split())[:800] if summary_el is not None else ""
        link = link_el.get("href") if link_el is not None else None

        items.append({
            "title": title,
            "summary": summary,
            "url": link or "",
            "published_at": _parse_dt(published_el.text if published_el is not None else None),
            "source_key": "arxiv",
            "entity_names": [a for a in authors if a],
        })
    return items


async def fetch_hn(*, limit: int = 20, feed: str = "topstories") -> list[dict]:
    """Hacker News Firebase API (§15.1) — no auth.

    Entity candidate is the linked article's domain (a reasonable company/product
    proxy for HN's link-heavy front page); text posts (Ask HN, etc.) carry no
    entity candidate and rely on corroboration from other sources' entity links,
    or simply score low and never clear the threshold alone.
    """
    base = "https://hacker-news.firebaseio.com/v0"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{base}/{feed}.json")
        resp.raise_for_status()
        ids = resp.json()[:limit]

        items: list[dict] = []
        for story_id in ids:
            item_resp = await client.get(f"{base}/item/{story_id}.json")
            if item_resp.status_code != 200:
                continue
            item = item_resp.json() or {}
            if item.get("type") != "story" or not item.get("title"):
                continue

            story_url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            entity_names = []
            if item.get("url"):
                host = urlparse(item["url"]).netloc.removeprefix("www.")
                if host:
                    entity_names.append(host)

            items.append({
                "title": item["title"],
                "summary": f"{item.get('score', 0)} points, {item.get('descendants', 0)} comments on HN.",
                "url": story_url,
                "published_at": (
                    datetime.fromtimestamp(item["time"], tz=timezone.utc) if item.get("time") else None
                ),
                "source_key": "hn",
                "entity_names": entity_names,
            })
    return items


async def fetch_federal_register(*, limit: int = 20) -> list[dict]:
    """Federal Register API (§15.1) — no auth. Entities are the issuing agencies."""
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {"per_page": str(limit), "order": "newest"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = []
    for doc in data.get("results", [])[:limit]:
        title = doc.get("title") or "[No title available]"
        agencies = [a.get("name") for a in (doc.get("agencies") or []) if a.get("name")]
        items.append({
            "title": title,
            "summary": (doc.get("abstract") or "")[:800],
            "url": doc.get("html_url") or "",
            "published_at": _parse_dt(doc.get("publication_date")),
            "source_key": "federal_register",
            "entity_names": agencies,
        })
    return items


async def fetch_wikipedia_recent_changes(*, limit: int = 20, user_agent: str) -> list[dict]:
    """Wikipedia RecentChanges API (§15.1) — no auth, but Wikimedia's User-Agent
    policy (https://meta.wikimedia.org/wiki/User-Agent_policy) rejects requests
    with no/anonymous User-Agent as 403 Forbidden, so one is required here too.

    The edited page's title IS the entity (a broad general-attention proxy via
    edit velocity). Bot edits excluded to cut noise per the spec's own warning
    that this source is "extremely broad, noisy."
    """
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcnamespace": "0",
        "rctype": "edit|new",
        "rcshow": "!bot",
        "rclimit": str(limit),
        "format": "json",
    }
    headers = {"User-Agent": user_agent}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    items: list[dict] = []
    for change in data.get("query", {}).get("recentchanges", [])[:limit]:
        title = change.get("title")
        if not title:
            continue
        page_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
        items.append({
            "title": f"Wikipedia edit: {title}",
            "summary": f"Recent edit activity on the '{title}' Wikipedia page.",
            "url": page_url,
            "published_at": _parse_dt(change.get("timestamp")),
            "source_key": "wikipedia_recent_changes",
            "entity_names": [title],
        })
    return items


FETCHERS = {
    "sec_edgar": fetch_sec_edgar,
    "arxiv": fetch_arxiv,
    "hn": fetch_hn,
    "federal_register": fetch_federal_register,
    "wikipedia_recent_changes": fetch_wikipedia_recent_changes,
}
