"""Plot collection tools — save/remove Mermaid (and other) plots to daily collections.

Architecture
~~~~~~~~~~~~
Chart data lives in **session metadata** (``sessions.metadata.plots``), not in
the moment.  The ``plot_collection`` moment is a lightweight feed pointer:

    moment  →  source_session_id  →  session.metadata = {"plots": [...]}

This means the agent automatically gets the chart context on session recovery
(via ``ChatController.prepare → session.metadata → ContextInjector``).

The Flutter ``PlotDetailScreen`` reads plots from ``moment.sessionMetadata``
which is joined via ``LEFT JOIN sessions`` on all moment endpoints.

Example moment (pointer only)::

    Moment(
        name="plots-ec932220-2026-02-28",
        moment_type="plot_collection",
        category="research",
        summary="Research (2): Microservices Patterns, Auth Flow",
        topic_tags=["microservices", "auth"],
        user_id=user_id,
        source_session_id=session_id,
        metadata={"plot_count": 2, "date": "2026-02-28"},
    )

Example session metadata::

    session.metadata = {
        "plots": [
            {
                "plot_id": "a1b2c3d4",
                "plot_type": "mermaid",
                "title": "Microservices Patterns",
                "source": "graph LR\\n  A-->B",
                "description": "...",
                "topic_tags": ["microservices"],
                "created_at": "2026-02-28T12:00:00+00:00",
            },
        ],
        "plot_count": 1,
        "date": "2026-02-28",
    }
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from p8.api.tools import get_db, get_encryption, get_session_id, get_user_id

_VALID_PLOT_TYPES = {"mermaid", "chartjs", "vega"}


def _collection_name(user_id_hex: str, day: date) -> str:
    return f"plots-{user_id_hex[:8]}-{day.isoformat()}"


def _build_summary(plots: list[dict]) -> str:
    titles = [p.get("title", "Untitled") for p in plots]
    return f"Research ({len(plots)}): {', '.join(titles)}"


def _collect_tags(plots: list[dict]) -> list[str]:
    tags: set[str] = set()
    for p in plots:
        for t in p.get("topic_tags", []):
            tags.add(t)
    return sorted(tags)


async def save_plot(
    title: str,
    source: str,
    plot_type: str = "mermaid",
    description: str | None = None,
    topic_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Save a plot to today's daily research collection.

    Chart data is stored in the **session** metadata so the agent recovers
    context on subsequent turns.  A lightweight ``plot_collection`` moment
    is upserted in the feed as a pointer (``source_session_id``).

    Args:
        title: Short title for the plot (e.g. "API Request Flow")
        source: Raw plot source code (e.g. Mermaid graph definition)
        plot_type: Plot format — "mermaid" (default), "chartjs", or "vega"
        description: Optional longer description of what the plot shows
        topic_tags: Optional tags for categorization

    Returns:
        Dict with status, plot_id, moment_id, plot_count, collection_name, moment_link
    """
    if plot_type not in _VALID_PLOT_TYPES:
        return {
            "status": "error",
            "error": f"Invalid plot_type '{plot_type}'. Must be one of: {', '.join(sorted(_VALID_PLOT_TYPES))}",
        }

    user_id = get_user_id()
    if not user_id:
        return {"status": "error", "error": "user_id is required for saving plots"}

    session_id = get_session_id()

    db = get_db()
    encryption = get_encryption()
    now = datetime.now(timezone.utc)
    today = now.date()
    collection_key = _collection_name(user_id.hex, today)

    # Build the plot item
    plot_id = uuid4().hex[:8]
    plot_item = {
        "plot_id": plot_id,
        "plot_type": plot_type,
        "title": title,
        "source": source,
        "description": description,
        "topic_tags": topic_tags or [],
        "created_at": now.isoformat(),
    }

    # ---- Store plots in SESSION metadata (source of truth) ----
    from p8.ontology.types import Moment, Session
    from p8.services.repository import Repository

    if session_id:
        session_repo = Repository(Session, db, encryption)
        session = await session_repo.get(session_id)
        if session:
            meta = session.metadata or {}
            plots = meta.get("plots", [])
            plots.append(plot_item)
            meta["plots"] = plots
            meta["plot_count"] = len(plots)
            meta["date"] = today.isoformat()
            await session_repo.merge_metadata(session_id, meta)
        else:
            # Session doesn't exist yet — create minimal one
            plots = [plot_item]
            session = Session(
                id=session_id,
                name=f"research-{today.isoformat()}",
                mode="research",
                user_id=user_id,
                metadata={
                    "plots": plots,
                    "plot_count": 1,
                    "date": today.isoformat(),
                },
            )
            await session_repo.upsert(session)
    else:
        # No session — read existing plots from the moment metadata directly
        existing_moment_row = await db.fetchrow(
            "SELECT id, metadata FROM moments "
            "WHERE name = $1 AND moment_type = 'plot_collection' AND deleted_at IS NULL",
            collection_key,
        )
        if existing_moment_row and existing_moment_row["metadata"]:
            meta = existing_moment_row["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            plots = meta.get("plots", [])
            plots.append(plot_item)
        else:
            plots = [plot_item]

    # ---- Upsert lightweight pointer moment in the feed ----
    moment_repo = Repository(Moment, db, encryption)
    all_tags = _collect_tags(plots)
    summary = _build_summary(plots)

    existing_row = await db.fetchrow(
        "SELECT id FROM moments "
        "WHERE name = $1 AND moment_type = 'plot_collection' AND deleted_at IS NULL",
        collection_key,
    )

    if existing_row:
        # Update the pointer (summary, tags, count, and plots if no session)
        moment_meta = {"plot_count": len(plots), "date": today.isoformat()}
        if not session_id:
            moment_meta["plots"] = plots
        await db.execute(
            "UPDATE moments SET summary = $1, topic_tags = $2, "
            "metadata = $3::jsonb, "
            "updated_at = NOW() WHERE id = $4",
            summary, all_tags, json.dumps(moment_meta), existing_row["id"],
        )
        moment_id = str(existing_row["id"])
    else:
        new_meta: dict[str, Any] = {
            "plot_count": len(plots),
            "date": today.isoformat(),
        }
        if not session_id:
            new_meta["plots"] = plots
        moment = Moment(
            name=collection_key,
            moment_type="plot_collection",
            category="research",
            summary=summary,
            topic_tags=all_tags,
            user_id=user_id,
            source_session_id=session_id,
            metadata=new_meta,
        )
        [saved] = await moment_repo.upsert(moment)
        moment_id = str(saved.id)

    return {
        "status": "success",
        "plot_id": plot_id,
        "moment_id": moment_id,
        "plot_count": len(plots),
        "collection_name": collection_key,
        "moment_link": f"moment://{collection_key}",
    }


async def remove_plot(
    plot_id: str,
    collection_date: str | None = None,
) -> dict[str, Any]:
    """Remove a plot from a daily collection by its plot_id.

    Removes from session metadata (source of truth) and updates the pointer moment.

    Args:
        plot_id: The 8-character hex ID of the plot to remove
        collection_date: ISO date string (e.g. "2026-02-27"). Defaults to today.

    Returns:
        Dict with status, removed_title, remaining_count
    """
    user_id = get_user_id()
    if not user_id:
        return {"status": "error", "error": "user_id is required for removing plots"}

    db = get_db()
    encryption = get_encryption()

    if collection_date:
        try:
            day = date.fromisoformat(collection_date)
        except ValueError:
            return {"status": "error", "error": f"Invalid date format: {collection_date}"}
    else:
        day = datetime.now(timezone.utc).date()

    collection_key = _collection_name(user_id.hex, day)

    # Find the pointer moment to get the session
    row = await db.fetchrow(
        "SELECT id, source_session_id FROM moments "
        "WHERE name = $1 AND moment_type = 'plot_collection' AND deleted_at IS NULL",
        collection_key,
    )
    if not row:
        return {"status": "error", "error": f"No plot collection found for {day.isoformat()}"}

    session_id = row["source_session_id"]
    if not session_id:
        return {"status": "error", "error": "No session linked to this collection"}

    # Load plots from session metadata
    from p8.ontology.types import Session
    from p8.services.repository import Repository

    session_repo = Repository(Session, db, encryption)
    session = await session_repo.get(session_id)
    if not session:
        return {"status": "error", "error": "Session not found"}

    plots = (session.metadata or {}).get("plots", [])

    # Find and remove the plot
    removed = None
    new_plots = []
    for p in plots:
        if p.get("plot_id") == plot_id:
            removed = p
        else:
            new_plots.append(p)

    if not removed:
        return {"status": "error", "error": f"Plot '{plot_id}' not found in collection for {day.isoformat()}"}

    # Update session metadata
    await session_repo.merge_metadata(session_id, {
        "plots": new_plots,
        "plot_count": len(new_plots),
    })

    # Update pointer moment
    summary = _build_summary(new_plots) if new_plots else "Empty research collection"
    all_tags = _collect_tags(new_plots)
    await db.execute(
        "UPDATE moments SET summary = $1, topic_tags = $2, "
        "metadata = jsonb_set(metadata, '{plot_count}', $3::jsonb), "
        "updated_at = NOW() WHERE id = $4",
        summary, all_tags, json.dumps(len(new_plots)), row["id"],
    )

    return {
        "status": "success",
        "removed_title": removed.get("title", "Untitled"),
        "remaining_count": len(new_plots),
    }
