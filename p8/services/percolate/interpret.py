"""LLM interpretation step (§16.4 stage 4) — the expensive, low-volume step,
only reached by events that already cleared the algorithmic threshold.

Writes the short "read on the toilet" blurb (§1's format target) and performs
the fresh/ongoing/saturated classification (§3.1's v1 build-scope note: using
only algorithmic signals already in the pipeline — corroboration score, source
diversity, entity-resolution confidence — never a live web search in v1).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)


class Interpretation(BaseModel):
    writeup: str
    classification: Literal["fresh_scoop", "ongoing_story", "saturated"]
    topics: list[str] = []


_SYSTEM_PROMPT = """\
You write short, opinionated 2-3 sentence blurbs for a "detected event" feed \
(think: something you'd read on the toilet or on the train — no fluff, no \
hedging preamble). You are given a primary source document (a filing, a \
paper, a regulatory notice, a Wikipedia edit, an HN link) plus algorithmic \
signals already computed by the pipeline: a corroboration score, which \
distinct independent source types confirmed it, and a suggested \
classification. You do NOT have live web search access — classify using only \
the given signals, adjusting the suggested classification only if the \
primary-source content itself clearly contradicts it. Never claim something \
is a "scoop" without the corroboration signal actually supporting it. Always \
ground the writeup in the primary source content given, never invent facts \
beyond it."""

_PROMPT_TEMPLATE = """\
Title: {title}
Primary source: {primary_source_url}
Excerpt: {excerpt}
Resolved entities: {entities}
Category: {category}
Corroboration score: {corroboration_score:.2f}
Distinct independent source types: {distinct_source_types}
Entity resolution confidence: {entity_resolution_confidence:.2f}
Algorithmic suggested classification: {suggested_classification}

Write the blurb and classification."""


async def interpret_event(
    *, title: str, primary_source_url: str, excerpt: str, entity_names: list[str],
    category: str | None, corroboration_score: float, distinct_source_types: list[str],
    entity_resolution_confidence: float, suggested_classification: str, settings,
) -> Interpretation | None:
    """Run the LLM interpretation step. Returns None on failure (caller falls
    back to the algorithmic classification + raw excerpt, per the codebase's
    convention of never letting an LLM failure fail the whole pipeline)."""
    try:
        from pydantic_ai import Agent

        agent = Agent(
            settings.percolate_interpret_model or settings.default_model,
            instructions=_SYSTEM_PROMPT,
            output_type=Interpretation,
        )
        prompt = _PROMPT_TEMPLATE.format(
            title=title,
            primary_source_url=primary_source_url,
            excerpt=(excerpt or "")[:1500],
            entities=", ".join(entity_names) or "none resolved",
            category=category or "unknown",
            corroboration_score=corroboration_score,
            distinct_source_types=", ".join(distinct_source_types) or "none",
            entity_resolution_confidence=entity_resolution_confidence,
            suggested_classification=suggested_classification,
        )
        result = await agent.run(prompt)
        return result.output
    except Exception:
        log.warning("LLM interpretation failed for %r, using algorithmic fallback", title, exc_info=True)
        return None
