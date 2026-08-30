"""Orchestrates the full detection pipeline (§16.4):

raw ingestion -> entity resolution -> corroboration scoring -> threshold ->
LLM interpretation, persisted as scored, linked nodes on the event/entity
graph (§8.2). Used by both the worker handler
(p8/workers/handlers/percolate.py) and the CLI (p8 percolate ingest) — the
CLI path is what proves the pipeline against real live sources.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from p8.ontology.base import CoreModel
from p8.ontology.types import Entity, Event, Topic
from p8.services.graph import merge_graph_edges
from p8.services.percolate.corroboration import classify_event, score_corroboration
from p8.services.percolate.entity_resolution import ResolvedEntity, resolve_entity_name
from p8.services.percolate.interpret import interpret_event
from p8.services.percolate.sources import FETCHERS
from p8.services.repository import Repository

log = logging.getLogger(__name__)


async def _add_reverse_edge(db, encryption, model_cls: type[CoreModel], name: str, edge: dict) -> None:
    """rem_traverse only walks *outgoing* edges, so entity/topic nodes need
    their own edge pointing back at an event to make it discoverable from
    the entity/topic side ("walk the graph" — topic page -> related events,
    entity page -> events it was mentioned in)."""
    table = model_cls.__table_name__
    row = await db.fetchrow(f"SELECT * FROM {table} WHERE name = $1 AND deleted_at IS NULL", name)
    if not row:
        return
    data = dict(row)
    data["graph_edges"] = merge_graph_edges(data.get("graph_edges") or [], [edge])
    entity = model_cls.model_validate(data)
    await Repository(model_cls, db, encryption).upsert(entity)


async def _load_sources(db) -> dict[str, dict]:
    rows = await db.fetch(
        "SELECT key, source_type, domain, reliability_weight, enabled "
        "FROM percolate_sources WHERE deleted_at IS NULL"
    )
    return {r["key"]: dict(r) for r in rows}


def _dedupe_key(entity_names: list[str], published_at: datetime | None) -> str:
    """Stable cluster key: resolved entity set + day. Repeat sightings of the
    same underlying cluster merge into one event row instead of duplicating."""
    day = (published_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    basis = "|".join(sorted(n.lower() for n in entity_names)) or "unresolved"
    digest = hashlib.sha1(f"{basis}:{day}".encode()).hexdigest()[:12]
    return f"evt-{digest}"


async def _fetch_items(source_key: str, *, settings, limit: int) -> list[dict]:
    fetcher = FETCHERS.get(source_key)
    if not fetcher:
        raise ValueError(f"Unknown percolate source_key: {source_key}")
    kwargs: dict = {"limit": limit}
    if source_key in ("sec_edgar", "wikipedia_recent_changes"):
        kwargs["user_agent"] = settings.percolate_user_agent
    return await fetcher(**kwargs)


async def process_raw_item(
    item: dict, *, db, encryption, embedding_service, settings, sources_meta: dict,
) -> dict:
    """Resolve entities for one raw item and cluster it into an Event row
    (create new, or merge into an existing candidate cluster)."""
    entity_names = (item.get("entity_names") or [])[:5]  # cap fan-out per item
    resolved: list[ResolvedEntity] = []
    for raw_name in entity_names:
        if not raw_name or not raw_name.strip():
            continue
        try:
            resolved.append(await resolve_entity_name(
                raw_name, db=db, encryption=encryption,
                embedding_service=embedding_service, settings=settings,
            ))
        except Exception:
            log.warning("Entity resolution failed for %r", raw_name, exc_info=True)

    dedupe_names = [r.name for r in resolved] or [item["title"][:60]]
    name_key = _dedupe_key(dedupe_names, item.get("published_at"))

    existing = await db.fetchrow(
        "SELECT * FROM percolate_events WHERE name = $1 AND deleted_at IS NULL", name_key,
    )

    source_meta = sources_meta.get(item["source_key"], {})
    new_contribution = {
        "source_key": item["source_key"],
        "source_type": source_meta.get("source_type", "unknown"),
        "url": item["url"],
        "published_at": item["published_at"].isoformat() if item.get("published_at") else None,
    }
    entity_confidence = (sum(r.confidence for r in resolved) / len(resolved)) if resolved else 0.5
    mention_edges = [{"target": r.name, "relation": "mentions", "weight": 1.0} for r in resolved]

    repo = Repository(Event, db, encryption)

    if existing:
        contributing = list(existing["contributing_sources"] or [])
        if not any(c.get("url") == new_contribution["url"] for c in contributing):
            contributing.append(new_contribution)
        score, distinct_types = score_corroboration(contributing, sources_meta)
        edges = merge_graph_edges(existing["graph_edges"] or [], mention_edges)
        event = Event(
            id=existing["id"],
            name=name_key,
            title=existing["title"],
            summary=existing["summary"],
            primary_source_url=existing["primary_source_url"],
            primary_source_excerpt=existing["primary_source_excerpt"],
            category=existing["category"],
            status=existing["status"],
            classification=existing["classification"],
            corroboration_score=score,
            distinct_source_types=distinct_types,
            contributing_sources=contributing,
            entity_resolution_confidence=max(entity_confidence, float(existing["entity_resolution_confidence"] or 0)),
            event_time=existing["event_time"],
            graph_edges=edges,
            created_at=existing["created_at"],
        )
        created = False
    else:
        contributing = [new_contribution]
        score, distinct_types = score_corroboration(contributing, sources_meta)
        topic_name = source_meta.get("domain") or "general"
        edges = [*mention_edges, {"target": topic_name, "relation": "about", "weight": 1.0}]
        event = Event(
            name=name_key,
            title=item["title"],
            summary=item.get("summary"),
            primary_source_url=item["url"],
            primary_source_excerpt=(item.get("summary") or "")[:800],
            category=topic_name,
            status="raw",
            corroboration_score=score,
            distinct_source_types=distinct_types,
            contributing_sources=contributing,
            entity_resolution_confidence=entity_confidence,
            event_time=item.get("published_at"),
            graph_edges=edges,
        )
        created = True

    [saved] = await repo.upsert(event)

    for r in resolved:
        try:
            await _add_reverse_edge(
                db, encryption, Entity, r.name,
                {"target": name_key, "relation": "mentioned_in", "weight": 1.0},
            )
        except Exception:
            log.warning("Failed to add reverse edge for entity %r", r.name, exc_info=True)
    if created:
        try:
            await _add_reverse_edge(
                db, encryption, Topic, event.category or "general",
                {"target": name_key, "relation": "has_event", "weight": 1.0},
            )
        except Exception:
            log.warning("Failed to add reverse edge for topic %r", event.category, exc_info=True)

    return {"event": saved, "created": created, "resolved_entities": resolved}


async def maybe_interpret_event(
    event: Event, resolved_entities: list[ResolvedEntity], *, db, encryption, settings,
) -> Event:
    """Threshold gate (§16.4 stage 3) + LLM interpretation (stage 4).

    Only events whose corroboration_score clears
    ``settings.percolate_corroboration_threshold`` are passed to the LLM —
    this is what keeps LLM spend proportional to genuinely interesting events.
    """
    if event.status == "interpreted" or event.corroboration_score < settings.percolate_corroboration_threshold:
        return event

    suggested = classify_event(
        event.distinct_source_types, event.corroboration_score, event.entity_resolution_confidence,
    )
    entity_names = [r.name for r in resolved_entities] if resolved_entities else [
        e["target"] for e in event.graph_edges if e.get("relation") == "mentions"
    ]

    interpretation = await interpret_event(
        title=event.title,
        primary_source_url=event.primary_source_url,
        excerpt=event.primary_source_excerpt or event.summary or "",
        entity_names=entity_names,
        category=event.category,
        corroboration_score=event.corroboration_score,
        distinct_source_types=event.distinct_source_types,
        entity_resolution_confidence=event.entity_resolution_confidence,
        suggested_classification=suggested,
        settings=settings,
    )

    if interpretation:
        event.summary = interpretation.writeup
        event.classification = interpretation.classification
        if interpretation.topics:
            topic_edges = [{"target": t, "relation": "about", "weight": 1.0} for t in interpretation.topics]
            event.graph_edges = merge_graph_edges(event.graph_edges, topic_edges)
            topic_repo = Repository(Topic, db, encryption)
            for t in interpretation.topics:
                try:
                    await topic_repo.upsert(Topic(name=t))
                    await _add_reverse_edge(
                        db, encryption, Topic, t,
                        {"target": event.name, "relation": "has_event", "weight": 1.0},
                    )
                except Exception:
                    log.warning("Failed to upsert topic %r", t, exc_info=True)
    else:
        event.classification = suggested

    event.status = "interpreted"
    repo = Repository(Event, db, encryption)
    [saved] = await repo.upsert(event)
    return saved


async def run_ingestion_cycle(
    *, db, encryption, embedding_service, settings, source_keys: list[str] | None = None, limit: int = 20,
) -> dict:
    """Top-level orchestrator: ingest -> resolve -> score -> threshold -> interpret.

    Returns a summary dict of what happened, per source and in total — used
    by both the worker handler and ``p8 percolate ingest`` (the manual
    verification path against real live sources).
    """
    sources_meta = await _load_sources(db)
    if source_keys is None:
        source_keys = [k for k, v in sources_meta.items() if v.get("enabled", True)]
    else:
        source_keys = [k for k in source_keys if k in sources_meta]

    stats: dict = {
        "sources": {}, "items_ingested": 0,
        "events_created": 0, "events_updated": 0, "events_interpreted": 0, "errors": [],
    }

    for source_key in source_keys:
        try:
            items = await _fetch_items(source_key, settings=settings, limit=limit)
        except Exception as e:
            log.warning("Ingestion failed for source %s", source_key, exc_info=True)
            stats["errors"].append(f"{source_key}: {e}")
            continue

        source_stats = {"items": len(items), "events_created": 0, "events_updated": 0, "events_interpreted": 0}

        for item in items:
            try:
                result = await process_raw_item(
                    item, db=db, encryption=encryption, embedding_service=embedding_service,
                    settings=settings, sources_meta=sources_meta,
                )
            except Exception:
                log.warning("Failed to process item %r from %s", item.get("title"), source_key, exc_info=True)
                continue

            stats["items_ingested"] += 1
            event = result["event"]
            prior_status = event.status
            if result["created"]:
                source_stats["events_created"] += 1
                stats["events_created"] += 1
            else:
                source_stats["events_updated"] += 1
                stats["events_updated"] += 1

            try:
                interpreted = await maybe_interpret_event(
                    event, result["resolved_entities"], db=db, encryption=encryption, settings=settings,
                )
                if prior_status != "interpreted" and interpreted.status == "interpreted":
                    source_stats["events_interpreted"] += 1
                    stats["events_interpreted"] += 1
            except Exception:
                log.warning("Interpretation failed for event %s", event.name, exc_info=True)

        stats["sources"][source_key] = source_stats

    return stats
