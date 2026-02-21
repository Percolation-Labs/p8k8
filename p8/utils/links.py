"""Verify markdown links in ontology files.

Scans markdown files for [text](target) links and checks that each target
resolves to either:
  1. Another markdown file in the ontology tree (by stem name)
  2. An entity key in the KV store (requires database connection)

Usage:
  from p8.utils.links import verify_links
  issues = verify_links("docs/ontology/")

  # Or with DB verification:
  issues = await verify_links_with_db("docs/ontology/", db)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Matches markdown links: [text](target)
# Excludes links inside inline code (`...`) by using a negative lookbehind
_LINK_RE = re.compile(r"(?<!`)\[([^\]]+)\]\(([^)]+)\)(?!`)")

# Strips inline code spans before link extraction
_INLINE_CODE_RE = re.compile(r"`[^`]+`")

# Targets to skip — URLs, anchors, images
_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:")


@dataclass
class LinkIssue:
    """A broken or unresolved link found in an ontology file."""

    file: str
    line: int
    target: str
    text: str
    message: str


@dataclass
class LinkReport:
    """Result of verifying links across ontology files."""

    total_links: int = 0
    resolved: int = 0
    skipped: int = 0
    issues: list[LinkIssue] = field(default_factory=list)

    @property
    def broken(self) -> int:
        return len(self.issues)

    @property
    def ok(self) -> bool:
        return self.broken == 0


def extract_links(text: str) -> list[tuple[int, str, str]]:
    """Extract (line_number, link_text, link_target) from markdown text.

    Skips links inside fenced code blocks (``` ... ```)."""
    results = []
    in_code_block = False
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        # Strip inline code spans so `[key](target)` isn't matched
        clean_line = _INLINE_CODE_RE.sub("", line)
        for match in _LINK_RE.finditer(clean_line):
            link_text, target = match.group(1), match.group(2)
            results.append((i, link_text, target))
    return results


def _collect_stems(root: Path) -> set[str]:
    """Collect all markdown filename stems under a directory."""
    stems = set()
    for md in root.rglob("*.md"):
        if md.name == "README.md":
            continue
        stems.add(md.stem)
    return stems


def verify_links(root: str | Path) -> LinkReport:
    """Verify all markdown links in an ontology directory.

    Checks that each [text](target) link resolves to another markdown
    file's stem name within the same ontology tree. Skips URLs and anchors.

    Args:
        root: Path to the ontology directory (e.g., "docs/ontology/")

    Returns:
        LinkReport with resolved count, skipped count, and any issues.
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    known_stems = _collect_stems(root)
    report = LinkReport()

    for md_file in sorted(root.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        links = extract_links(text)

        for line_no, link_text, target in links:
            report.total_links += 1

            # Skip URLs, anchors, etc.
            if any(target.startswith(p) for p in _SKIP_PREFIXES):
                report.skipped += 1
                continue

            # Normalize: strip path prefix, get the stem
            target_stem = Path(target).stem

            if target_stem in known_stems:
                report.resolved += 1
            else:
                report.issues.append(
                    LinkIssue(
                        file=str(md_file.relative_to(root)),
                        line=line_no,
                        target=target,
                        text=link_text,
                        message=f"Target '{target}' (stem '{target_stem}') not found in ontology",
                    )
                )

    return report


async def verify_links_with_db(
    root: str | Path,
    db,
) -> LinkReport:
    """Verify links against both local files and the KV store.

    Extends verify_links() by checking unresolved targets against the
    database KV store. Requires an active database connection.
    """
    report = verify_links(root)

    if not report.issues:
        return report

    # Check remaining broken links against KV store
    still_broken = []
    for issue in report.issues:
        target_key = Path(issue.target).stem
        rows = await db.fetch(
            "SELECT entity_key FROM kv_store WHERE entity_key = $1 LIMIT 1",
            target_key,
        )
        if rows:
            report.resolved += 1
        else:
            still_broken.append(issue)

    report.issues = still_broken
    return report
