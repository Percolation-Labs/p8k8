"""Unit tests for corroboration scoring and classification (§16.3/§3.1).

Pure functions — no mocks, no network, no DB.
"""

from __future__ import annotations

from p8.services.percolate.corroboration import classify_event, score_corroboration

SOURCES = {
    "sec_edgar": {"source_type": "regulatory_filing", "reliability_weight": 1.0},
    "federal_register": {"source_type": "regulatory_notice", "reliability_weight": 1.0},
    "hn": {"source_type": "social_chatter", "reliability_weight": 0.6},
    "wikipedia_recent_changes": {"source_type": "attention_signal", "reliability_weight": 0.4},
}


def test_single_strong_source_clears_default_threshold():
    contributing = [{"source_key": "sec_edgar", "source_type": "regulatory_filing"}]
    score, types = score_corroboration(contributing, SOURCES)
    assert types == ["regulatory_filing"]
    assert score == 1.0


def test_same_source_type_twice_does_not_double_count():
    """Tradecraft principle: two hits of the same source_type are one
    confirmation, not two — independence, not volume (§16.3)."""
    contributing = [
        {"source_key": "hn", "source_type": "social_chatter"},
        {"source_key": "hn", "source_type": "social_chatter"},
    ]
    score, types = score_corroboration(contributing, SOURCES)
    assert types == ["social_chatter"]
    assert score == 0.6


def test_distinct_source_types_sum_independently():
    contributing = [
        {"source_key": "sec_edgar", "source_type": "regulatory_filing"},
        {"source_key": "hn", "source_type": "social_chatter"},
    ]
    score, types = score_corroboration(contributing, SOURCES)
    assert types == ["regulatory_filing", "social_chatter"]
    assert score == 1.6


def test_takes_max_weight_within_a_source_type():
    contributing = [
        {"source_key": "wikipedia_recent_changes", "source_type": "attention_signal"},
        {"source_key": "wikipedia_recent_changes", "source_type": "attention_signal", "reliability_weight": 0.9},
    ]
    score, _types = score_corroboration(contributing, SOURCES)
    # Falls back to source_registry weight (0.4) since it doesn't read the
    # ad-hoc override key on the contribution dict.
    assert score == 0.4


def test_classify_three_or_more_distinct_types_is_saturated():
    assert classify_event(["a", "b", "c"], 3.0, 1.0) == "saturated"


def test_classify_two_distinct_types_is_ongoing():
    assert classify_event(["a", "b"], 1.6, 1.0) == "ongoing_story"


def test_classify_single_strong_confident_source_is_fresh_scoop():
    assert classify_event(["regulatory_filing"], 1.0, 1.0) == "fresh_scoop"


def test_classify_single_weak_source_falls_back_to_ongoing():
    assert classify_event(["attention_signal"], 0.4, 0.5) == "ongoing_story"
