"""Integration tests for save_moments tool — requires running PostgreSQL."""

from __future__ import annotations

import json

import pytest

from p8.api.tools import init_tools, set_tool_context
from p8.api.tools.save_moments import save_moments
from p8.ontology.types import Moment, Resource
from p8.services.repository import Repository

from tests.integration.dreaming.fixtures import (
    RESOURCE_ML,
    TEST_USER_ID,
    setup_dreaming_fixtures,
)


@pytest.fixture(autouse=True)
async def _init(clean_db, db, encryption):
    """Initialize tools and fixtures."""
    init_tools(db, encryption)
    set_tool_context(user_id=TEST_USER_ID)
    await setup_dreaming_fixtures(db, encryption)
    yield


async def test_save_single_dream_moment(db, encryption):
    """Save one dream moment → verify it exists with moment_type='dream'."""
    result = await save_moments(
        moments=[{
            "name": "dream-test-insight",
            "summary": "Test insight about ML and architecture overlap.",
            "topic_tags": ["ml", "architecture"],
            "emotion_tags": ["curious"],
        }],
        user_id=TEST_USER_ID,
    )

    assert result["status"] == "success"
    assert result["moments_count"] == 1

    repo = Repository(Moment, db, encryption)
    moments = await repo.find(
        user_id=TEST_USER_ID,
        filters={"name": "Test Insight", "moment_type": "dream"},
    )
    assert len(moments) >= 1
    m = moments[0]
    assert m.summary == "Test insight about ML and architecture overlap."
    assert "ml" in m.topic_tags
    assert m.metadata["source"] == "dreaming"


async def test_save_moment_with_affinity_creates_graph_edges(db, encryption):
    """Save dream with affinity fragments → verify graph_edges on the moment."""
    result = await save_moments(
        moments=[{
            "name": "dream-affinity-test",
            "summary": "Links ML pipeline to architecture decisions.",
            "topic_tags": ["ml", "architecture"],
            "affinity_fragments": [
                {
                    "target": RESOURCE_ML,
                    "relation": "thematic_link",
                    "weight": 0.8,
                    "reason": "Both discuss pipeline optimization",
                },
            ],
        }],
        user_id=TEST_USER_ID,
    )

    assert result["status"] == "success"

    repo = Repository(Moment, db, encryption)
    moments = await repo.find(
        user_id=TEST_USER_ID,
        filters={"name": "Affinity Test"},
    )
    assert len(moments) >= 1
    m = moments[0]

    # Check graph_edges on the dream moment
    assert len(m.graph_edges) == 1
    edge = m.graph_edges[0]
    assert edge["target"] == RESOURCE_ML
    assert edge["relation"] == "thematic_link"
    assert edge["weight"] == 0.8
    assert edge["reason"] == "Both discuss pipeline optimization"


async def test_save_moment_creates_back_edge_on_target(db, encryption):
    """Save dream with affinity → verify 'dreamed_from' back-edge on target resource."""
    await save_moments(
        moments=[{
            "name": "dream-back-edge-test",
            "summary": "Test back-edge creation.",
            "topic_tags": ["test"],
            "affinity_fragments": [
                {
                    "target": RESOURCE_ML,
                    "relation": "thematic_link",
                    "weight": 0.7,
                    "reason": "Related topics",
                },
            ],
        }],
        user_id=TEST_USER_ID,
    )

    # Check the resource now has a dreamed_from back-edge
    repo = Repository(Resource, db, encryption)
    resources = await repo.find(
        user_id=TEST_USER_ID,
        filters={"name": RESOURCE_ML},
    )
    assert len(resources) >= 1
    r = resources[0]

    dreamed_edges = [e for e in r.graph_edges if e.get("relation") == "dreamed_from"]
    assert len(dreamed_edges) >= 1
    assert any(e["target"] == "Back Edge Test" for e in dreamed_edges)


async def test_merge_preserves_existing_edges_on_target(db, encryption):
    """Existing resource edges are preserved when dreaming adds a back-edge."""
    # The fixture resource_a already has an edge from moment_a
    repo = Repository(Resource, db, encryption)
    resources_before = await repo.find(
        user_id=TEST_USER_ID,
        filters={"name": RESOURCE_ML},
    )
    initial_edge_count = len(resources_before[0].graph_edges) if resources_before else 0

    await save_moments(
        moments=[{
            "name": "dream-preserve-test",
            "summary": "Should not overwrite existing edges.",
            "topic_tags": ["test"],
            "affinity_fragments": [
                {"target": RESOURCE_ML, "relation": "builds_on", "weight": 0.6, "reason": "test"},
            ],
        }],
        user_id=TEST_USER_ID,
    )

    resources_after = await repo.find(
        user_id=TEST_USER_ID,
        filters={"name": RESOURCE_ML},
    )
    r = resources_after[0]

    # Should have original edges + new dreamed_from edge
    assert len(r.graph_edges) >= initial_edge_count + 1


async def test_save_multiple_moments(db, encryption):
    """Save 3 dream moments at once → verify all persisted."""
    result = await save_moments(
        moments=[
            {"name": "dream-multi-1", "summary": "First insight.", "topic_tags": ["a"]},
            {"name": "dream-multi-2", "summary": "Second insight.", "topic_tags": ["b"]},
            {"name": "dream-multi-3", "summary": "Third insight.", "topic_tags": ["c"]},
        ],
        user_id=TEST_USER_ID,
    )

    assert result["status"] == "success"
    assert result["moments_count"] == 3

    repo = Repository(Moment, db, encryption)
    for name in ("Multi 1", "Multi 2", "Multi 3"):
        moments = await repo.find(user_id=TEST_USER_ID, filters={"name": name})
        assert len(moments) >= 1


async def test_affinity_target_not_found_graceful(db, encryption):
    """Affinity to a non-existent entity logs warning but doesn't fail."""
    result = await save_moments(
        moments=[{
            "name": "dream-missing-target",
            "summary": "Links to non-existent entity.",
            "topic_tags": ["test"],
            "affinity_fragments": [
                {"target": "does-not-exist", "relation": "related", "weight": 0.5, "reason": "test"},
            ],
        }],
        user_id=TEST_USER_ID,
    )

    assert result["status"] == "success"
    assert result["moments_count"] == 1
    # Merge result should show not_found
    assert any(
        mr.get("status") == "not_found" for mr in result["merge_results"]
    )
