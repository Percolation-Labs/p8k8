"""Tests for ontology ingestion — markdown, structured, and resource upsert paths.

Unit tests with mocked services following the pattern from tests/content/test_content.py.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from p8.ontology.types import Ontology, Resource, Schema
from p8.services.content import BulkUpsertResult, ContentService, load_structured
from p8.settings import Settings


# ============================================================================
# Helpers
# ============================================================================


def _make_content_service() -> tuple[ContentService, MagicMock, MagicMock]:
    """Create a ContentService with mocked DB and encryption."""
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()

    encryption = MagicMock()
    encryption.get_dek = AsyncMock(return_value=b"fake-key")
    encryption.encrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)
    encryption.decrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)

    file_service = MagicMock()
    file_service.read_text = AsyncMock(return_value="# Test Content\n\nBody text.")
    file_service.list_dir = MagicMock(return_value=[])

    settings = MagicMock(spec=Settings)
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200

    svc = ContentService(
        db=db, encryption=encryption, file_service=file_service, settings=settings
    )
    return svc, db, file_service


# ============================================================================
# Markdown → Ontologies
# ============================================================================


class TestMarkdownUpsert:
    @pytest.mark.asyncio
    async def test_single_file_creates_ontology(self):
        """A single .md file becomes one Ontology row with name=stem."""
        svc, _, file_service = _make_content_service()
        file_service.read_text = AsyncMock(return_value="# Overview\n\nREM query system.")

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_markdown(["/docs/ontology/rem-queries/overview.md"])

        assert result.count == 1
        assert result.table == "ontologies"
        assert captured[0].name == "overview"
        assert "REM query system" in captured[0].content

    @pytest.mark.asyncio
    async def test_multiple_files_create_multiple_ontologies(self):
        """A list of .md files creates one Ontology per file."""
        svc, _, file_service = _make_content_service()

        call_count = 0

        async def read_text(path):
            nonlocal call_count
            call_count += 1
            return f"# Page {call_count}\n\nContent for page {call_count}."

        file_service.read_text = AsyncMock(side_effect=read_text)

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_markdown([
                "/docs/ontology/rem-queries/lookup.md",
                "/docs/ontology/rem-queries/search.md",
                "/docs/ontology/rem-queries/fuzzy.md",
            ])

        assert result.count == 3
        names = {e.name for e in captured}
        assert names == {"lookup", "search", "fuzzy"}

    @pytest.mark.asyncio
    async def test_deterministic_ids_are_idempotent(self):
        """Same file name always produces the same entity ID."""
        svc, _, file_service = _make_content_service()
        file_service.read_text = AsyncMock(return_value="# Content")

        ids = []

        async def mock_upsert(entities):
            ids.extend(e.id for e in entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            await svc.upsert_markdown(["/docs/overview.md"])
            await svc.upsert_markdown(["/docs/overview.md"])

        assert len(ids) == 2
        assert ids[0] == ids[1], "Same file stem should produce same deterministic ID"

    @pytest.mark.asyncio
    async def test_tenant_id_stamped(self):
        """Tenant ID is applied to all ontology entities."""
        svc, _, file_service = _make_content_service()
        file_service.read_text = AsyncMock(return_value="# Content")

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            await svc.upsert_markdown(
                ["/docs/overview.md"], tenant_id="acme-corp"
            )

        assert captured[0].tenant_id == "acme-corp"

    @pytest.mark.asyncio
    async def test_custom_model_class(self):
        """upsert_markdown can target a different table via model_class."""
        svc, _, file_service = _make_content_service()
        file_service.read_text = AsyncMock(return_value="# Resource content")

        async def mock_upsert(entities):
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_markdown(
                ["/docs/chunk.md"], model_class=Resource
            )

        assert result.table == "resources"


# ============================================================================
# Structured Data → Tables
# ============================================================================


class TestStructuredUpsert:
    @pytest.mark.asyncio
    async def test_yaml_list_upserts_schemas(self):
        """A YAML list of schemas creates one Schema per item."""
        svc, _, _ = _make_content_service()

        items = [
            {"name": "query-agent", "kind": "agent", "description": "Answers questions"},
            {"name": "summarizer", "kind": "agent", "description": "Summarizes text"},
        ]

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_structured(items, Schema)

        assert result.count == 2
        assert result.table == "schemas"
        names = {e.name for e in captured}
        assert names == {"query-agent", "summarizer"}

    @pytest.mark.asyncio
    async def test_tenant_and_user_stamped(self):
        """Structured upsert applies default tenant_id and user_id."""
        svc, _, _ = _make_content_service()
        uid = UUID("00000000-0000-0000-0000-000000000042")

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            await svc.upsert_structured(
                [{"name": "test", "kind": "model"}],
                Schema,
                tenant_id="t1",
                user_id=uid,
            )

        assert captured[0].tenant_id == "t1"
        assert captured[0].user_id == uid


# ============================================================================
# CLI upsert command
# ============================================================================


class TestUpsertCLI:
    def test_markdown_folder_upsert(self):
        """p8 upsert <dir> upserts all .md files as ontologies."""
        from typer.testing import CliRunner
        from p8.api.cli import app

        runner = CliRunner()

        # Create temp dir with .md files
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "page-a.md").write_text("# Page A\nContent A.")
            (Path(tmpdir) / "page-b.md").write_text("# Page B\nContent B.")

            mock_result = BulkUpsertResult(count=2, table="ontologies")

            with patch("p8.api.cli.upsert._run_upsert", new_callable=AsyncMock) as mock_run:
                # We patch the async runner to avoid needing a real DB
                mock_run.return_value = None

                # Patch asyncio.run to call our mock
                with patch("p8.api.cli.upsert.asyncio") as mock_asyncio:
                    mock_asyncio.run = MagicMock()
                    result = runner.invoke(app, ["upsert", tmpdir])

                assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_structured_requires_table(self):
        """_run_upsert with YAML and no table name prints error and exits."""
        from p8.api.cli.upsert import _run_upsert
        import typer

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write("- name: test\n  kind: model\n")
            f.flush()
            yaml_path = f.name

        try:
            # Mock bootstrap_services to provide fake services
            mock_db = MagicMock()
            mock_enc = MagicMock()
            mock_settings = MagicMock()
            mock_file_svc = MagicMock()
            mock_content_svc = MagicMock()
            mock_embed_svc = None

            class _MockCtx:
                async def __aenter__(self):
                    return (mock_db, mock_enc, mock_settings, mock_file_svc, mock_content_svc, mock_embed_svc)
                async def __aexit__(self, *a):
                    pass

            with patch("p8.api.cli.upsert._svc.bootstrap_services", return_value=_MockCtx()):
                with pytest.raises(typer.Exit) as exc_info:
                    await _run_upsert(None, yaml_path, None, None)
                assert exc_info.value.exit_code == 1
        finally:
            Path(yaml_path).unlink(missing_ok=True)


# ============================================================================
# load_structured edge cases
# ============================================================================


class TestLoadStructuredOntology:
    def test_single_ontology_yaml(self):
        """A single ontology dict wraps to a list."""
        result = load_structured(
            'name: overview\ncontent: "REM overview"',
            "data.yaml",
        )
        assert len(result) == 1
        assert result[0]["name"] == "overview"

    def test_list_of_ontologies(self):
        """A list of ontology dicts stays as-is."""
        result = load_structured(
            "- name: lookup\n  content: Lookup mode\n- name: search\n  content: Search mode\n",
            "data.yaml",
        )
        assert len(result) == 2
