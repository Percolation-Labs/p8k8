"""Tests for link verification in ontology files."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from p8.utils.links import (
    LinkIssue,
    LinkReport,
    extract_links,
    verify_links,
    verify_links_with_db,
)


# ============================================================================
# extract_links
# ============================================================================


class TestExtractLinks:
    def test_basic_link(self):
        links = extract_links("See [LOOKUP](lookup) for details.")
        assert len(links) == 1
        assert links[0] == (1, "LOOKUP", "lookup")

    def test_multiple_links_same_line(self):
        links = extract_links("Use [LOOKUP](lookup) or [SEARCH](search).")
        assert len(links) == 2

    def test_multiline(self):
        text = "# Title\n\nSee [A](a).\n\nAlso [B](b)."
        links = extract_links(text)
        assert len(links) == 2
        assert links[0] == (3, "A", "a")
        assert links[1] == (5, "B", "b")

    def test_url_links_included(self):
        """extract_links returns all links — filtering is done by verify_links."""
        links = extract_links("[docs](https://example.com)")
        assert len(links) == 1
        assert links[0][2] == "https://example.com"

    def test_no_links(self):
        links = extract_links("No links here, just text.")
        assert len(links) == 0

    def test_path_targets(self):
        links = extract_links("See [Overview](rem-queries/overview).")
        assert links[0][2] == "rem-queries/overview"


# ============================================================================
# verify_links — filesystem only
# ============================================================================


class TestVerifyLinks:
    def test_all_links_resolve(self):
        """All internal links point to existing .md file stems."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "overview.md").write_text("See [Lookup](lookup) and [Search](search).")
            (root / "lookup.md").write_text("# Lookup\n\nBack to [Overview](overview).")
            (root / "search.md").write_text("# Search\n\nSee [Overview](overview).")

            report = verify_links(tmpdir)

        assert report.ok
        assert report.total_links == 4
        assert report.resolved == 4
        assert report.broken == 0

    def test_broken_link_detected(self):
        """A link to a non-existent target is reported as broken."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "overview.md").write_text("See [Missing](nonexistent).")
            (root / "lookup.md").write_text("# Lookup\n\nContent.")

            report = verify_links(tmpdir)

        assert not report.ok
        assert report.broken == 1
        assert report.issues[0].target == "nonexistent"
        assert report.issues[0].file == "overview.md"
        assert report.issues[0].line == 1

    def test_urls_skipped(self):
        """HTTP(S) URLs are skipped, not checked as internal links."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text(
                "See [docs](https://example.com) and [code](http://example.com)."
            )

            report = verify_links(tmpdir)

        assert report.ok
        assert report.skipped == 2
        assert report.broken == 0

    def test_anchors_skipped(self):
        """Anchor links (#section) are skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text("See [section](#details).")

            report = verify_links(tmpdir)

        assert report.ok
        assert report.skipped == 1

    def test_path_prefix_resolved(self):
        """Links with path prefixes like 'rem-queries/lookup' resolve by stem."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            subdir = root / "rem-queries"
            subdir.mkdir()
            (subdir / "overview.md").write_text("See [Lookup](lookup).")
            (subdir / "lookup.md").write_text("Back to [Overview](overview).")

            report = verify_links(tmpdir)

        assert report.ok
        assert report.resolved == 2

    def test_readme_excluded_from_stems(self):
        """README.md files are not included in the stem set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("See [Overview](overview).")
            (root / "overview.md").write_text("# Overview")

            report = verify_links(tmpdir)

        # The README links to overview which exists
        assert report.ok

    def test_not_a_directory_raises(self):
        """verify_links raises ValueError for non-directory paths."""
        with pytest.raises(ValueError, match="Not a directory"):
            verify_links("/nonexistent/path")

    def test_mixed_links(self):
        """Mix of valid, broken, and URL links."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text(
                "Links: [A](a), [B](missing), [C](https://example.com)."
            )
            (root / "a.md").write_text("# A")

            report = verify_links(tmpdir)

        assert report.total_links == 3
        assert report.resolved == 1
        assert report.skipped == 1
        assert report.broken == 1


# ============================================================================
# verify_links_with_db
# ============================================================================


class TestVerifyLinksWithDb:
    @pytest.mark.asyncio
    async def test_broken_link_resolved_by_kv_store(self):
        """A link broken in filesystem can be resolved via KV store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text("See [Agent](query-agent).")

            # Mock DB that finds "query-agent" in kv_store
            db = MagicMock()
            db.fetch = AsyncMock(return_value=[{"entity_key": "query-agent"}])

            report = await verify_links_with_db(tmpdir, db)

        assert report.ok
        assert report.resolved == 1
        assert report.broken == 0

    @pytest.mark.asyncio
    async def test_still_broken_after_db_check(self):
        """A link not in filesystem or KV store remains broken."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text("See [Missing](truly-missing).")

            db = MagicMock()
            db.fetch = AsyncMock(return_value=[])  # not in KV store

            report = await verify_links_with_db(tmpdir, db)

        assert not report.ok
        assert report.broken == 1
        assert report.issues[0].target == "truly-missing"

    @pytest.mark.asyncio
    async def test_no_db_call_when_all_resolved(self):
        """When all links resolve locally, no DB queries are made."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.md").write_text("See [B](b).")
            (root / "b.md").write_text("See [A](a).")

            db = MagicMock()
            db.fetch = AsyncMock()

            report = await verify_links_with_db(tmpdir, db)

        assert report.ok
        db.fetch.assert_not_awaited()


# ============================================================================
# verify-links CLI command
# ============================================================================


class TestVerifyLinksCLI:
    def test_clean_ontology_exits_0(self):
        """p8 verify-links on a clean ontology exits successfully."""
        from typer.testing import CliRunner
        from p8.api.cli import app

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.md").write_text("See [B](b).")
            (root / "b.md").write_text("See [A](a).")

            result = runner.invoke(app, ["verify-links", tmpdir])

        assert result.exit_code == 0
        assert "0 broken" in result.output

    def test_broken_links_exits_1(self):
        """p8 verify-links with broken links exits with code 1."""
        from typer.testing import CliRunner
        from p8.api.cli import app

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "page.md").write_text("See [Missing](nonexistent).")

            result = runner.invoke(app, ["verify-links", tmpdir])

        assert result.exit_code == 1
        assert "1 broken" in result.output


# ============================================================================
# Verify the actual demo ontology
# ============================================================================


class TestDemoOntology:
    def test_demo_ontology_links_resolve(self):
        """All links in docs/ontology/ resolve to known stems."""
        ontology_dir = Path(__file__).parent.parent.parent.parent / "docs" / "ontology"
        if not ontology_dir.is_dir():
            pytest.skip("docs/ontology/ not found")

        report = verify_links(str(ontology_dir))

        if not report.ok:
            for issue in report.issues:
                print(f"  {issue.file}:{issue.line}  [{issue.text}]({issue.target})")

        assert report.ok, f"{report.broken} broken link(s) in demo ontology"
        assert report.total_links > 0, "Demo ontology should have links"
