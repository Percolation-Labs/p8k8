"""Unit tests for merge_graph_edges — pure function, no DB required."""

from p8.services.graph import merge_graph_edges


def test_merge_no_overlap():
    existing = [{"target": "a", "relation": "related", "weight": 0.5}]
    new = [{"target": "b", "relation": "related", "weight": 0.7}]
    result = merge_graph_edges(existing, new)
    assert len(result) == 2
    targets = {e["target"] for e in result}
    assert targets == {"a", "b"}


def test_merge_dedup_keeps_higher_weight():
    existing = [{"target": "a", "relation": "related", "weight": 0.5}]
    new = [{"target": "a", "relation": "related", "weight": 0.9}]
    result = merge_graph_edges(existing, new)
    assert len(result) == 1
    assert result[0]["weight"] == 0.9


def test_merge_dedup_keeps_existing_when_higher():
    existing = [{"target": "a", "relation": "related", "weight": 0.9}]
    new = [{"target": "a", "relation": "related", "weight": 0.3}]
    result = merge_graph_edges(existing, new)
    assert len(result) == 1
    assert result[0]["weight"] == 0.9


def test_merge_different_relations_not_deduped():
    existing = [{"target": "a", "relation": "references", "weight": 0.5}]
    new = [{"target": "a", "relation": "builds_on", "weight": 0.7}]
    result = merge_graph_edges(existing, new)
    assert len(result) == 2


def test_merge_empty_existing():
    new = [{"target": "a", "relation": "related", "weight": 0.5}]
    result = merge_graph_edges([], new)
    assert len(result) == 1
    assert result[0]["target"] == "a"


def test_merge_empty_new():
    existing = [{"target": "a", "relation": "related", "weight": 0.5}]
    result = merge_graph_edges(existing, [])
    assert len(result) == 1
    assert result[0]["target"] == "a"


def test_merge_both_empty():
    assert merge_graph_edges([], []) == []


def test_merge_preserves_all_fields():
    existing = [{"target": "x", "relation": "builds_on", "weight": 0.8}]
    result = merge_graph_edges(existing, [])
    e = result[0]
    assert e["target"] == "x"
    assert e["relation"] == "builds_on"
    assert e["weight"] == 0.8


def test_merge_defaults_relation():
    """Edges without explicit relation default to 'related' for dedup key."""
    existing = [{"target": "a", "weight": 0.5}]
    new = [{"target": "a", "weight": 0.9}]
    result = merge_graph_edges(existing, new)
    assert len(result) == 1
    assert result[0]["weight"] == 0.9
