"""Graph edge utilities — merge, dedup, filter operations on graph_edges JSONB."""

from __future__ import annotations


def merge_graph_edges(
    existing: list[dict],
    new_edges: list[dict],
) -> list[dict]:
    """Merge new graph edges into existing, deduplicating by (target, relation).

    When a (target, relation) pair already exists, keep the higher weight.
    New edges that don't conflict are appended.

    Args:
        existing: Current graph_edges list from the entity.
        new_edges: New edges to merge in.

    Returns:
        Merged list of graph edges (no duplicates by target+relation).
    """
    edge_map: dict[tuple[str, str], dict] = {}

    for e in existing:
        key = (e.get("target", ""), e.get("relation", "related"))
        if key not in edge_map or e.get("weight", 1.0) > edge_map[key].get("weight", 1.0):
            edge_map[key] = e

    for e in new_edges:
        key = (e.get("target", ""), e.get("relation", "related"))
        if key not in edge_map or e.get("weight", 1.0) > edge_map[key].get("weight", 1.0):
            edge_map[key] = e

    return list(edge_map.values())
