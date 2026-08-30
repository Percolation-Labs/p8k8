"""Corroboration scoring (§16.3) — independence, not volume.

Tradecraft principle: corroboration only counts when sources are
*structurally independent*. Two items from the same source_type are treated
as one confirmation, not two — three HN stories about the same company don't
outweigh a single SEC filing. A candidate event's score sums the reliability
weight of each *distinct* source_type confirming it (taking the strongest
source within a type, not summing duplicates within it).

**v1 judgment call (flagged per the issue's request to surface open questions
rather than guess silently):** the spec leaves the exact threshold formula
combining corroboration score, source reliability, and entity-resolution
confidence as an open question (§23) pending real event volume. With v1's five
low-overlap, domain-diverse sources, requiring multi-source-type corroboration
for *everything* would produce almost no interpreted events in a short-window
demo run — the sources rarely double-cover the same narrow entity cluster
within a single poll cycle by design (finance filings vs. arXiv papers vs. HN
links vs. Federal Register notices vs. Wikipedia edits). So v1 lets a single
high-reliability primary-source hit with a confidently-resolved entity clear
the threshold on its own (weight 1.0 x confidence 1.0 = 1.0, comfortably above
the 0.6 default), while low-reliability/poorly-resolved single hits (e.g. a
Wikipedia edit-velocity signal linked to an ambiguous entity) do not. Real
multi-source-type corroboration still scores higher and is what pushes an
event toward "saturated" in classify_event() below. This should be revisited
once there's enough real ingestion volume to calibrate against actual
duplicate coverage, per the spec's own note that this needs real data.
"""

from __future__ import annotations


def score_corroboration(
    contributing_sources: list[dict], source_registry: dict[str, dict],
) -> tuple[float, list[str]]:
    """Score a candidate event's corroboration strength.

    Args:
        contributing_sources: [{"source_key": ..., "source_type": ..., ...}, ...]
        source_registry: source_key -> {"source_type": ..., "reliability_weight": ...}

    Returns:
        (corroboration_score, distinct_source_types) — score is the sum of the
        max reliability_weight per distinct source_type represented.
    """
    best_weight_by_type: dict[str, float] = {}
    for src in contributing_sources:
        meta = source_registry.get(src.get("source_key", ""), {})
        source_type = src.get("source_type") or meta.get("source_type", "unknown")
        weight = float(meta.get("reliability_weight", src.get("reliability_weight", 0.5)))
        if source_type not in best_weight_by_type or weight > best_weight_by_type[source_type]:
            best_weight_by_type[source_type] = weight

    score = sum(best_weight_by_type.values())
    distinct_source_types = sorted(best_weight_by_type)
    return score, distinct_source_types


def classify_event(
    distinct_source_types: list[str], corroboration_score: float, entity_resolution_confidence: float,
) -> str:
    """Algorithmic fresh/ongoing/saturated classification (§3.1's v1 build-scope note).

    No live search in v1 — uses only signals already in the pipeline. More
    distinct independent source types confirming the same entity cluster
    implies wider existing coverage, so it's treated as *less* fresh, not more
    corroborated-as-a-scoop — that's the inversion the spec's tariff example
    (§3.1) warns about: wide corroboration is itself evidence something is
    already covered, not evidence it's a bigger scoop.
    """
    n = len(distinct_source_types)
    if n >= 3:
        return "saturated"
    if n == 2:
        return "ongoing_story"
    if corroboration_score >= 0.6 and entity_resolution_confidence >= 0.7:
        return "fresh_scoop"
    return "ongoing_story"
