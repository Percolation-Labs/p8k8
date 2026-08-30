"""Entity resolution (§16.2) — a cascading, cost-ordered process.

Tier 1: canonical registry match (exact name/alias, free).
Tier 2: Wikidata crosswalk (free, zero-auth API).
Tier 3: embedding fallback via pgvector (already a project dependency).
Tier 4: LLM resolution (last resort — only genuinely ambiguous cases).

Each tier escalates only when the cheaper tier fails, matching the
cost-optimized routing philosophy in §18.3. Every successful resolution
persists (or confirms) a row in ``percolate_entities`` so the *next* mention
of the same entity resolves at tier 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from p8.ontology.types import Entity
from p8.services.repository import Repository

log = logging.getLogger(__name__)

_WIKIDATA_URL = "https://www.wikidata.org/w/api.php"


@dataclass
class ResolvedEntity:
    name: str  # canonical name
    tier: str  # registry | wikidata | embedding | llm
    confidence: float
    entity_type: str | None = None
    external_ids: dict = field(default_factory=dict)


def _resolution_text(name: str, aliases: list[str] | None, description: str | None) -> str:
    parts = [name, *(aliases or [])]
    if description:
        parts.append(description)
    return " — ".join(p for p in parts if p)


async def _upsert_entity(
    db, encryption, *, name: str, tier: str, confidence: float,
    entity_type: str | None = None, external_ids: dict | None = None,
    description: str | None = None, aliases: list[str] | None = None,
) -> ResolvedEntity:
    repo = Repository(Entity, db, encryption)
    entity = Entity(
        name=name,
        entity_type=entity_type,
        aliases=aliases or [],
        external_ids=external_ids or {},
        description=description,
        resolution_text=_resolution_text(name, aliases, description),
        resolution_tier=tier,
        resolution_confidence=confidence,
    )
    await repo.upsert(entity)
    return ResolvedEntity(
        name=name, tier=tier, confidence=confidence,
        entity_type=entity_type, external_ids=external_ids or {},
    )


async def _match_registry(raw_name: str, db) -> ResolvedEntity | None:
    row = await db.fetchrow(
        "SELECT name, entity_type, external_ids FROM percolate_entities "
        "WHERE deleted_at IS NULL AND (lower(name) = lower($1) OR $1 = ANY(aliases)) "
        "LIMIT 1",
        raw_name,
    )
    if not row:
        return None
    return ResolvedEntity(
        name=row["name"], tier="registry", confidence=1.0,
        entity_type=row["entity_type"], external_ids=row["external_ids"] or {},
    )


async def _match_wikidata(raw_name: str, db, encryption) -> ResolvedEntity | None:
    params = {
        "action": "wbsearchentities", "search": raw_name, "language": "en",
        "format": "json", "limit": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_WIKIDATA_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.warning("Wikidata lookup failed for %r", raw_name, exc_info=True)
        return None

    hits = data.get("search") or []
    if not hits:
        return None
    hit = hits[0]
    label = hit.get("label") or raw_name
    qid = hit.get("id")
    description = hit.get("description")

    return await _upsert_entity(
        db, encryption, name=label, tier="wikidata", confidence=0.85,
        external_ids={"wikidata_qid": qid} if qid else {},
        description=description,
        aliases=[raw_name] if raw_name.lower() != label.lower() else [],
    )


async def _match_embedding(raw_name: str, db, embedding_service, settings) -> ResolvedEntity | None:
    if embedding_service is None:
        return None
    try:
        vectors = await embedding_service.provider.embed([raw_name])
    except Exception:
        log.warning("Embedding fallback failed for %r", raw_name, exc_info=True)
        return None
    if not vectors or not vectors[0]:
        return None

    results = await db.rem_search(
        vectors[0], "percolate_entities", field="resolution_text",
        provider=embedding_service.provider.provider_name,
        min_similarity=settings.percolate_embedding_match_threshold,
        limit=1,
    )
    if not results:
        return None
    row = results[0]
    data = row.get("data") or {}
    name = data.get("name") if isinstance(data, dict) else None
    if not name:
        return None
    similarity = float(row.get("similarity_score", settings.percolate_embedding_match_threshold))
    return ResolvedEntity(
        name=name, tier="embedding", confidence=min(similarity, 0.95),
        entity_type=data.get("entity_type"), external_ids=data.get("external_ids") or {},
    )


async def _resolve_llm(raw_name: str, db, encryption, settings) -> ResolvedEntity:
    """Last resort — clean up the raw name into a canonical form via a cheap model.

    Genuinely the minority path: only reached when registry, Wikidata, and
    embedding fallback all miss. Re-checks the registry after cleanup in case
    normalization (e.g. "Acme Corp." -> "Acme Corporation") reveals an existing
    canonical entity the raw string didn't match exactly.
    """
    from pydantic import BaseModel

    class _Resolution(BaseModel):
        canonical_name: str
        entity_type: str | None = None

    canonical_name = raw_name.strip()
    entity_type = None
    try:
        from pydantic_ai import Agent

        agent = Agent(
            settings.percolate_interpret_model or settings.default_model,
            instructions=(
                "You clean up noisy entity name strings extracted from news/filing "
                "sources into a canonical display name. Fix casing and strip boilerplate "
                "suffixes/prefixes but do not invent facts. Classify the entity_type as "
                "one of: company, person, product, government_body, other."
            ),
            output_type=_Resolution,
        )
        result = await agent.run(f"Raw entity string: {raw_name!r}")
        parsed = result.output
        canonical_name = parsed.canonical_name.strip() or canonical_name
        entity_type = parsed.entity_type
    except Exception:
        log.warning("LLM entity resolution failed for %r, using raw name", raw_name, exc_info=True)

    if canonical_name.lower() != raw_name.lower():
        existing = await _match_registry(canonical_name, db)
        if existing:
            return existing

    return await _upsert_entity(
        db, encryption, name=canonical_name, tier="llm", confidence=0.6,
        entity_type=entity_type, aliases=[raw_name] if raw_name != canonical_name else [],
    )


async def resolve_entity_name(
    raw_name: str, *, db, encryption, embedding_service, settings,
) -> ResolvedEntity:
    """Resolve a raw entity name string to a canonical entity, escalating tiers."""
    raw_name = raw_name.strip()

    resolved = await _match_registry(raw_name, db)
    if resolved:
        return resolved

    resolved = await _match_wikidata(raw_name, db, encryption)
    if resolved:
        return resolved

    resolved = await _match_embedding(raw_name, db, embedding_service, settings)
    if resolved:
        return resolved

    return await _resolve_llm(raw_name, db, encryption, settings)
