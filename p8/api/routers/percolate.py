"""Read-only Percolate detection-core surface (§9.2's v1 build-scope note):

GET /feed and GET /topics/{id} equivalents — browse detected/interpreted
events and walk the event/entity graph. No marketplace write endpoints
(claim/takes/comments/ratings/subscribe) — those are out of scope for v1.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from p8.api.deps import get_db, get_encryption
from p8.ontology.types import Entity, Event, Topic
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository

router = APIRouter()


@router.get("/feed")
async def percolate_feed(
    status: str = Query("interpreted", description="raw | thresholded | interpreted"),
    category: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db),
):
    """The main scroll — interpreted events, ranked by corroboration score
    then recency (§9.2's read-only GET /feed equivalent)."""
    conditions = ["deleted_at IS NULL", "status = $1"]
    args: list = [status]
    if category:
        args.append(category)
        conditions.append(f"category = ${len(args)}")
    args.extend([limit, offset])
    where = " AND ".join(conditions)

    rows = await db.fetch(
        f"""SELECT * FROM percolate_events WHERE {where}
            ORDER BY corroboration_score DESC, event_time DESC NULLS LAST
            LIMIT ${len(args) - 1} OFFSET ${len(args)}""",
        *args,
    )
    return [dict(r) for r in rows]


@router.get("/topics")
async def list_topics(db: Database = Depends(get_db), encryption: EncryptionService = Depends(get_encryption)):
    """Browse the topic list (one per source domain in v1, plus any the LLM
    interpretation step introduces)."""
    repo = Repository(Topic, db, encryption)
    topics = await repo.find(limit=100)
    return [t.model_dump(mode="json") for t in topics]


@router.get("/topics/{topic_id}")
async def get_topic(
    topic_id: UUID, db: Database = Depends(get_db), encryption: EncryptionService = Depends(get_encryption),
):
    """A topic page: related events plus, one hop further, the entities those
    events mention — both derived by walking the event/entity graph (§8.2)
    via rem_traverse rather than a bespoke join."""
    repo = Repository(Topic, db, encryption)
    topic = await repo.get(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    related = await db.rem_traverse(topic.name, max_depth=2, load=True)
    events = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_events" and r.get("entity_record")]
    entities = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_entities" and r.get("entity_record")]

    return {"topic": topic.model_dump(mode="json"), "events": events, "entities": entities}


@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: UUID, db: Database = Depends(get_db), encryption: EncryptionService = Depends(get_encryption),
):
    """An entity page: events it was mentioned in, plus one hop further, the
    topics those events belong to."""
    repo = Repository(Entity, db, encryption)
    entity = await repo.get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    related = await db.rem_traverse(entity.name, max_depth=2, load=True)
    events = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_events" and r.get("entity_record")]
    topics = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_topics" and r.get("entity_record")]

    return {"entity": entity.model_dump(mode="json"), "events": events, "topics": topics}


@router.get("/events/{event_id}")
async def get_event(
    event_id: UUID, db: Database = Depends(get_db), encryption: EncryptionService = Depends(get_encryption),
):
    """An event's detail — including the resolved entities/topics linked to
    it and, per §16.5, the primary source link every event carries."""
    repo = Repository(Event, db, encryption)
    event = await repo.get(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    related = await db.rem_traverse(event.name, max_depth=1, load=True)
    entities = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_entities" and r.get("entity_record")]
    topics = [r["entity_record"] for r in related if r.get("entity_type") == "percolate_topics" and r.get("entity_record")]

    return {"event": event.model_dump(mode="json"), "entities": entities, "topics": topics}
