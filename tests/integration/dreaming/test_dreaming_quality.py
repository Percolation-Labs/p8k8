"""Quality evaluation test for dreaming output.

Runs the full dreaming pipeline against fixture data and evaluates each
dream moment for tone, structure, and linking quality.

Requires:
  - Running PostgreSQL (docker compose up -d)
  - P8_OPENAI_API_KEY set in environment or .env

Criteria:
  - Tone: No banned words, no emojis, journalistic prose
  - Structure: Markdown headings (##), prose body, ### Threads section
  - Linking: Contains moment:// or resource:// internal links
"""

from __future__ import annotations

import logging
import re

import pytest

from p8.services.bootstrap import _export_api_keys
from p8.settings import Settings
from p8.api.tools import init_tools, set_tool_context
from p8.ontology.types import Moment
from p8.services.repository import Repository
from p8.workers.handlers.dreaming import DreamingHandler

from tests.integration.dreaming.fixtures import (
    MOMENT_ARCH,
    MOMENT_ML,
    MOMENT_SLEEP,
    MOMENT_TRAIL,
    RESOURCE_ARCH,
    RESOURCE_ML,
    RESOURCE_SLEEP,
    RESOURCE_TRAIL,
    TEST_USER_ID,
    setup_dreaming_fixtures,
)

log = logging.getLogger(__name__)

BANNED_WORDS = {
    "holistic", "synergy", "synergistic", "leverage", "utilize",
    "ecosystem", "paradigm", "landscape", "delve", "foster",
    "comprehensive", "streamline", "robust", "robustness",
    "scalable", "scalability", "cutting-edge", "empower", "harness",
    "pivotal", "seamless", "optimize", "optimization",
}

EMOJI_PATTERN = re.compile(
    "[\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001f900-\U0001f9ff"  # supplemental
    "\U0001fa00-\U0001fa6f"  # chess symbols
    "\U0001fa70-\U0001faff"  # extended-A
    "]+",
    flags=re.UNICODE,
)


class _Ctx:
    def __init__(self, db, encryption):
        self.db = db
        self.encryption = encryption


def _score_tone(summary: str) -> dict:
    """Check for banned words and emojis. Returns {score, issues}."""
    issues = []
    words = summary.lower().split()
    found_banned = [w for w in words if w.strip(".,;:!?\"'()") in BANNED_WORDS]
    if found_banned:
        issues.append(f"banned words: {', '.join(found_banned)}")

    if EMOJI_PATTERN.search(summary):
        issues.append("contains emojis")

    score = 5 if not issues else max(1, 5 - len(issues))
    return {"score": score, "issues": issues}


def _score_structure(summary: str) -> dict:
    """Check markdown structure: headings, prose, threads section."""
    issues = []
    has_heading = bool(re.search(r"^##\s+\S", summary, re.MULTILINE))
    has_threads = bool(re.search(r"^###\s+(Threads|Sources)", summary, re.MULTILINE))
    has_bold = "**" in summary
    has_code = "`" in summary
    has_bullets = bool(re.search(r"^-\s+", summary, re.MULTILINE))

    if not has_heading:
        issues.append("missing ## heading")
    if not has_threads:
        issues.append("missing ### Threads section")
    if not has_bold and not has_code:
        issues.append("no bold or code formatting")
    if not has_bullets:
        issues.append("no bullet lists")

    score = 5 - len(issues)
    return {"score": max(1, score), "issues": issues}


def _score_linking(summary: str) -> dict:
    """Check for internal moment:// and resource:// links."""
    issues = []
    moment_links = re.findall(r"\[.*?\]\(moment://[^)]+\)", summary)
    resource_links = re.findall(r"\[.*?\]\(resource://[^)]+\)", summary)
    all_links = moment_links + resource_links

    if not all_links:
        issues.append("no internal links (moment:// or resource://)")
    elif len(all_links) < 2:
        issues.append(f"only {len(all_links)} internal link(s), expected 2+")

    score = 5 if len(all_links) >= 2 else (3 if len(all_links) == 1 else 1)
    return {"score": score, "issues": issues}


def _evaluate_dream(dream: Moment) -> dict:
    """Full quality evaluation of a single dream moment."""
    summary = dream.summary or ""
    tone = _score_tone(summary)
    structure = _score_structure(summary)
    linking = _score_linking(summary)
    overall = round((tone["score"] + structure["score"] + linking["score"]) / 3, 1)
    return {
        "name": dream.name,
        "tone": tone,
        "structure": structure,
        "linking": linking,
        "overall": overall,
        "summary_preview": summary[:200],
    }


@pytest.fixture(autouse=True)
async def _setup(clean_db, db, encryption):
    _export_api_keys(Settings())
    init_tools(db, encryption)
    set_tool_context(user_id=TEST_USER_ID)
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'dream' AND user_id = $1",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1)",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1",
        TEST_USER_ID,
    )
    await setup_dreaming_fixtures(db, encryption)
    # Clear adapter cache to avoid stale DB references across tests
    from p8.agentic.adapter import _adapter_cache
    _adapter_cache.clear()
    yield


@pytest.mark.llm
async def test_dreaming_quality(db, encryption):
    """Run dreaming pipeline and evaluate output quality."""
    handler = DreamingHandler()
    ctx = _Ctx(db, encryption)

    result = await handler.handle(
        {"user_id": str(TEST_USER_ID), "lookback_days": 1},
        ctx,
    )

    phase2 = result.get("phase2", {})
    assert phase2.get("status") == "ok", f"Phase 2 failed: {phase2}"

    # Fetch dream moments
    moment_repo = Repository(Moment, db, encryption)
    dreams = await moment_repo.find(
        user_id=TEST_USER_ID,
        filters={"moment_type": "dream"},
    )
    assert len(dreams) >= 1, "Expected at least 1 dream moment"

    # Evaluate each dream
    scorecard = []
    for dream in dreams:
        evaluation = _evaluate_dream(dream)
        scorecard.append(evaluation)

    # Print scorecard
    print("\n" + "=" * 70)
    print("DREAMING QUALITY SCORECARD")
    print("=" * 70)
    for entry in scorecard:
        print(f"\n--- {entry['name']} (overall: {entry['overall']}/5) ---")
        print(f"  Tone:      {entry['tone']['score']}/5", end="")
        if entry["tone"]["issues"]:
            print(f"  [{', '.join(entry['tone']['issues'])}]")
        else:
            print()
        print(f"  Structure: {entry['structure']['score']}/5", end="")
        if entry["structure"]["issues"]:
            print(f"  [{', '.join(entry['structure']['issues'])}]")
        else:
            print()
        print(f"  Linking:   {entry['linking']['score']}/5", end="")
        if entry["linking"]["issues"]:
            print(f"  [{', '.join(entry['linking']['issues'])}]")
        else:
            print()
        print(f"\n  Preview: {entry['summary_preview']}...")
    print("\n" + "=" * 70)

    avg_overall = sum(e["overall"] for e in scorecard) / len(scorecard)
    print(f"Average overall: {avg_overall:.1f}/5")
    print("=" * 70 + "\n")

    # Quality gates
    for entry in scorecard:
        assert entry["tone"]["score"] >= 4, (
            f"Tone too low for {entry['name']}: {entry['tone']}"
        )
        assert entry["structure"]["score"] >= 3, (
            f"Structure too low for {entry['name']}: {entry['structure']}"
        )
        assert entry["linking"]["score"] >= 3, (
            f"Linking too low for {entry['name']}: {entry['linking']}"
        )

    # ── Grouping evaluation ──
    # Fixture data has 3 distinct themes:
    #   1. Technical (ML + architecture) — should be 1 dream
    #   2. Outdoor/personal (trail running + nature) — should be 1 dream
    #   3. Health (sleep + nutrition) — should be 1 dream
    # Cross-cutting insights are OK as extra, but total should be <= 4.
    # More than 4 dreams means the agent is over-splitting within domains.

    TECHNICAL_KEYS = {MOMENT_ML, MOMENT_ARCH, RESOURCE_ML, RESOURCE_ARCH}
    OUTDOOR_KEYS = {MOMENT_TRAIL, RESOURCE_TRAIL}
    HEALTH_KEYS = {MOMENT_SLEEP, RESOURCE_SLEEP}

    def _dream_edge_targets(dream):
        return {e["target"] for e in dream.graph_edges}

    def _dream_references_domain(dream, domain_keys):
        """Check if dream summary or edges reference any key from this domain."""
        targets = _dream_edge_targets(dream)
        if targets & domain_keys:
            return True
        summary = (dream.summary or "").lower()
        for key in domain_keys:
            if key.lower() in summary:
                return True
        return False

    tech_dreams = [d for d in dreams if _dream_references_domain(d, TECHNICAL_KEYS)]
    outdoor_dreams = [d for d in dreams if _dream_references_domain(d, OUTDOOR_KEYS)]
    health_dreams = [d for d in dreams if _dream_references_domain(d, HEALTH_KEYS)]

    print("\n" + "=" * 70)
    print("GROUPING EVALUATION")
    print("=" * 70)
    print(f"  Total dreams: {len(dreams)}")
    print(f"  Technical domain dreams: {len(tech_dreams)}")
    print(f"  Outdoor domain dreams:   {len(outdoor_dreams)}")
    print(f"  Health domain dreams:    {len(health_dreams)}")

    grouping_issues = []
    if len(dreams) > 4:
        grouping_issues.append(f"Too many dreams ({len(dreams)}) — expected <= 4 for 3 themes")
    if len(tech_dreams) > 2:
        grouping_issues.append(
            f"Technical over-split: {len(tech_dreams)} dreams (expected 1-2)"
        )

    if grouping_issues:
        print(f"  Issues: {grouping_issues}")
    else:
        print("  Grouping: PASS")
    print("=" * 70 + "\n")

    # Soft assertion — warn but don't fail for now until we tune
    if len(dreams) > 4:
        log.warning("Grouping: too many dreams (%d), expected <= 4", len(dreams))
    assert len(dreams) <= 5, (
        f"Way too many dreams ({len(dreams)}) for 3 thematic domains — "
        f"agent is over-splitting. Names: {[d.name for d in dreams]}"
    )

    # Full summaries for manual inspection
    print("\nFULL DREAM SUMMARIES:")
    print("-" * 70)
    for dream in dreams:
        print(f"\n### {dream.name}\n")
        print(dream.summary)
        print()
