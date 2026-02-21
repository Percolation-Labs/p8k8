"""save_moments tool — persist dream moments and merge graph edges."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from p8.api.tools import get_db, get_encryption, get_session_id, get_user_id
from p8.ontology.types import Moment
from p8.services.graph import merge_graph_edges
from p8.services.repository import Repository

log = logging.getLogger(__name__)


async def save_moments(
    moments: list[dict[str, Any]],
    user_id: UUID | None = None,
) -> dict[str, Any]:
    """Save dream moments to the database and merge graph edges on related entities.

    Each moment dict should contain:
      - name: str — kebab-case identifier (e.g. "dream-ml-patterns")
      - summary: str — 2-4 sentence insight
      - topic_tags: list[str] — relevant topics
      - emotion_tags: list[str] — emotional tones (optional)
      - affinity_fragments: list[dict] — each with:
          - target: str — key of the related entity (moment or resource name)
          - relation: str — type of relationship (e.g. "thematic_link", "builds_on")
          - weight: float — strength of connection (0.0-1.0)
          - reason: str — why this connection exists

    Args:
        moments: List of dream moment definitions.
        user_id: User who owns these dream moments.

    Returns:
        Result dict with saved moment IDs and merge status.
    """
    db = get_db()
    encryption = get_encryption()
    # Fall back to context user_id if not provided by caller
    if user_id is None:
        user_id = get_user_id()
    repo = Repository(Moment, db, encryption)

    saved_ids = []
    merge_results = []

    for m in moments:
        # Extract affinity fragments → convert to graph_edges
        affinities = m.pop("affinity_fragments", []) or []
        graph_edges = [
            {
                "target": a["target"],
                "relation": a.get("relation", "dream_affinity"),
                "weight": a.get("weight", 0.5),
                "reason": a.get("reason", ""),
            }
            for a in affinities
            if a.get("target")
        ]

        # Ensure dream moment names are prefixed with "dream-"
        raw_name = m.get("name", "unnamed")
        name = raw_name if raw_name.startswith("dream-") else f"dream-{raw_name}"

        moment = Moment(
            name=name,
            moment_type="dream",
            summary=m.get("summary", ""),
            topic_tags=m.get("topic_tags", []),
            emotion_tags=m.get("emotion_tags", []),
            graph_edges=graph_edges,
            user_id=user_id,
            source_session_id=get_session_id(),
            metadata={"source": "dreaming"},
        )
        [saved] = await repo.upsert(moment)
        saved_ids.append(str(saved.id))

        # Merge back-edges on related entities (bidirectional links)
        for a in affinities:
            target_key = a.get("target")
            if not target_key:
                continue
            back_edge = {
                "target": saved.name,
                "relation": "dreamed_from",
                "weight": a.get("weight", 0.5),
                "reason": a.get("reason", ""),
            }
            try:
                result = await _merge_edge_on_target(db, target_key, back_edge)
                merge_results.append(result)
            except Exception as e:
                log.warning("Failed to merge edge on %s: %s", target_key, e)
                merge_results.append(
                    {"target": target_key, "status": "error", "error": str(e)}
                )

    return {
        "status": "success",
        "saved_moment_ids": saved_ids,
        "moments_count": len(saved_ids),
        "merge_results": merge_results,
    }


async def _merge_edge_on_target(
    db, target_key: str, new_edge: dict,
) -> dict:
    """Look up an entity by key in kv_store, merge a new edge onto it.

    Uses kv_store for O(1) key resolution, then updates the source table.
    """
    rows = await db.fetch(
        "SELECT entity_type, entity_id, graph_edges FROM kv_store"
        " WHERE entity_key = $1 LIMIT 1",
        target_key,
    )
    if not rows:
        return {"target": target_key, "status": "not_found"}

    row = rows[0]
    entity_type = row["entity_type"]
    entity_id = row["entity_id"]
    existing_edges = row["graph_edges"] or []
    if isinstance(existing_edges, str):
        existing_edges = json.loads(existing_edges)

    merged = merge_graph_edges(existing_edges, [new_edge])

    # Update the source table
    await db.execute(
        f"UPDATE {entity_type} SET graph_edges = $1::jsonb WHERE id = $2",
        json.dumps(merged),
        entity_id,
    )
    # Also update kv_store for consistency
    await db.execute(
        "UPDATE kv_store SET graph_edges = $1::jsonb WHERE entity_key = $2",
        json.dumps(merged),
        target_key,
    )

    return {"target": target_key, "status": "merged", "edge_count": len(merged)}
