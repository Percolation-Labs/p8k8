"""Unit tests for ontology/verify.py — DDL verification and model registration (all mocked, no DB)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from p8.ontology.types import ALL_ENTITY_TYPES, KV_TABLES, Schema, Server
from p8.ontology.verify import (
    Issue,
    _build_json_schema,
    _derive_kv_summary,
    verify_all,
    verify_model,
    register_models,
)

from typer.testing import CliRunner

from p8.api.cli import app
from tests.unit.helpers import MockAsyncServices

runner = CliRunner()


# ============================================================================
# Unit tests — _derive_kv_summary
# ============================================================================


class TestDeriveKvSummary:
    def test_schemas_table(self):
        """schemas has content + description + name, not encrypted → COALESCE(content, description, name)."""
        assert _derive_kv_summary(Schema) == "COALESCE(content, description, name)"

    def test_server_no_embeddings(self):
        """servers has description + name → COALESCE(description, name)."""
        assert _derive_kv_summary(Server) == "COALESCE(description, name)"

    def test_encrypted_content_uses_name(self):
        """Models with encrypted content field should use 'name' for KV summary."""
        from p8.ontology.types import Ontology

        assert _derive_kv_summary(Ontology) == "name"

    def test_non_kv_table_returns_none(self):
        """Tables not in KV_TABLES should return None."""
        from p8.ontology.types import StorageGrant

        assert _derive_kv_summary(StorageGrant) is None

    def test_feedback_not_in_kv(self):
        """Feedback is not in KV_TABLES → None."""
        from p8.ontology.types import Feedback

        assert _derive_kv_summary(Feedback) is None


# ============================================================================
# Unit tests — _build_json_schema
# ============================================================================


class TestBuildJsonSchema:
    def test_schema_model(self):
        meta = _build_json_schema(Schema)
        assert meta["has_kv_sync"] is True
        assert meta["has_embeddings"] is True
        assert meta["embedding_field"] == "description"
        assert meta["is_encrypted"] is False
        assert meta["kv_summary_expr"] == "COALESCE(content, description, name)"

    def test_server_no_embeddings(self):
        meta = _build_json_schema(Server)
        assert meta["has_embeddings"] is False
        assert meta["embedding_field"] is None

    def test_all_models_produce_valid_meta(self):
        """Every model in ALL_ENTITY_TYPES should produce a dict with all 5 keys."""
        for model in ALL_ENTITY_TYPES:
            meta = _build_json_schema(model)
            assert set(meta.keys()) == {"has_kv_sync", "has_embeddings", "embedding_field", "is_encrypted", "kv_summary_expr"}


# ============================================================================
# Unit tests — verify_model with mocked DB
# ============================================================================


def _make_mock_db(
    *,
    table_exists=True,
    columns=None,
    embed_table_exists=None,
    schema_row=None,
    schema_row_missing=False,
    triggers=None,
):
    """Build a mock db that responds to verify_model's queries."""
    db = MagicMock()

    call_count = {"fetchval": 0}

    async def mock_fetchval(query, *args):
        call_count["fetchval"] += 1
        if "information_schema.tables" in query:
            table_name = args[0]
            if table_name.startswith("embeddings_"):
                return embed_table_exists if embed_table_exists is not None else table_exists
            return table_exists
        return None

    db.fetchval = AsyncMock(side_effect=mock_fetchval)

    async def mock_fetch(query, *args):
        if "information_schema.columns" in query:
            return [{"column_name": c} for c in (columns or [])]
        if "information_schema.triggers" in query:
            return [{"trigger_name": t} for t in (triggers or [])]
        return []

    db.fetch = AsyncMock(side_effect=mock_fetch)

    async def mock_fetchrow(query, *args):
        if "schemas" in query and "kind = 'table'" in query:
            if schema_row_missing:
                return None
            if schema_row is not None:
                return schema_row
            return {"json_schema": json.dumps(_build_json_schema(Schema))}
        return None

    db.fetchrow = AsyncMock(side_effect=mock_fetchrow)

    return db


class TestVerifyModelMocked:
    @pytest.mark.asyncio
    async def test_missing_table(self):
        db = _make_mock_db(table_exists=False)
        issues = await verify_model(Schema, db)
        assert any(i.check == "missing_table" for i in issues)
        assert all(i.level == "error" for i in issues)

    @pytest.mark.asyncio
    async def test_missing_column(self):
        """Model declares columns not in DB → error."""
        db = _make_mock_db(
            columns=["id", "created_at", "updated_at"],
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        missing = [i for i in issues if i.check == "missing_column"]
        assert len(missing) > 0
        missing_names = {i.message.split("'")[1] for i in missing}
        assert "name" in missing_names
        assert "kind" in missing_names

    @pytest.mark.asyncio
    async def test_extra_column_is_warning(self):
        """DB has column not in model → warning."""
        all_cols = list(Schema.model_fields.keys()) + ["legacy_col"]
        db = _make_mock_db(
            columns=all_cols,
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        extras = [i for i in issues if i.check == "extra_column"]
        assert len(extras) == 1
        assert extras[0].level == "warning"
        assert "legacy_col" in extras[0].message

    @pytest.mark.asyncio
    async def test_missing_embedding_table(self):
        """Model has __embedding_field__ but embeddings table doesn't exist → error."""
        db = _make_mock_db(
            columns=list(Schema.model_fields.keys()),
            embed_table_exists=False,
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        assert any(i.check == "missing_embedding_table" for i in issues)

    @pytest.mark.asyncio
    async def test_stale_embedding_table_warning(self):
        """Model has no __embedding_field__ but embeddings table exists → warning."""
        db = _make_mock_db(
            columns=list(Server.model_fields.keys()),
            embed_table_exists=True,
            schema_row={"json_schema": json.dumps(_build_json_schema(Server))},
            triggers=["trg_servers_updated_at", "trg_servers_kv"],
        )
        issues = await verify_model(Server, db)
        stale = [i for i in issues if i.check == "stale_embedding_table"]
        assert len(stale) == 1
        assert stale[0].level == "warning"

    @pytest.mark.asyncio
    async def test_unregistered_schema(self):
        """No schemas row for this table → error."""
        db = _make_mock_db(
            columns=list(Schema.model_fields.keys()),
            schema_row_missing=True,
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        assert any(i.check == "unregistered_schema" for i in issues)

    @pytest.mark.asyncio
    async def test_schema_metadata_mismatch(self):
        """Schema row json_schema doesn't match model → error."""
        bad_meta = _build_json_schema(Schema)
        bad_meta["has_embeddings"] = False  # wrong
        db = _make_mock_db(
            columns=list(Schema.model_fields.keys()),
            schema_row={"json_schema": json.dumps(bad_meta)},
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        mismatches = [i for i in issues if i.check == "schema_metadata_mismatch"]
        assert len(mismatches) >= 1
        assert any("has_embeddings" in i.message for i in mismatches)

    @pytest.mark.asyncio
    async def test_missing_trigger(self):
        """Expected trigger not installed → error."""
        db = _make_mock_db(
            columns=list(Schema.model_fields.keys()),
            triggers=["trg_schemas_updated_at"],  # missing kv, embed, timemachine
        )
        issues = await verify_model(Schema, db)
        missing = [i for i in issues if i.check == "missing_trigger"]
        assert len(missing) >= 1
        trigger_names = {i.message.split("'")[1] for i in missing}
        assert "trg_schemas_kv" in trigger_names

    @pytest.mark.asyncio
    async def test_clean_model_no_issues(self):
        """A model with all expected DB state should produce no issues."""
        all_cols = list(Schema.model_fields.keys())
        meta = _build_json_schema(Schema)
        db = _make_mock_db(
            columns=all_cols,
            embed_table_exists=True,
            schema_row={"json_schema": json.dumps(meta)},
            triggers=["trg_schemas_updated_at", "trg_schemas_kv", "trg_schemas_embed", "trg_schemas_timemachine"],
        )
        issues = await verify_model(Schema, db)
        assert len(issues) == 0


# ============================================================================
# Unit tests — verify_all
# ============================================================================


class TestVerifyAll:
    @pytest.mark.asyncio
    async def test_iterates_all_models(self):
        """verify_all should check every model in ALL_ENTITY_TYPES."""
        checked_tables = []

        async def mock_verify(model, db):
            checked_tables.append(model.__table_name__)
            return []

        with patch("p8.ontology.verify.verify_model", side_effect=mock_verify):
            await verify_all(MagicMock())

        expected = [m.__table_name__ for m in ALL_ENTITY_TYPES]
        assert checked_tables == expected


# ============================================================================
# Unit tests — CLI commands (mocked)
# ============================================================================


class TestVerifyCLI:
    def test_verify_no_issues(self):
        mock = MockAsyncServices()
        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.schema.verify_all", new_callable=AsyncMock, return_value=[]),
        ):
            result = runner.invoke(app, ["schema", "verify"])
        assert result.exit_code == 0
        assert "0 error(s)" in result.output

    def test_verify_with_errors_exits_1(self):
        mock = MockAsyncServices()
        issues = [Issue("schemas", "error", "missing_column", "Column 'foo' missing")]
        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.schema.verify_all", new_callable=AsyncMock, return_value=issues),
        ):
            result = runner.invoke(app, ["schema", "verify"])
        assert result.exit_code == 1
        assert "1 error(s)" in result.output
        assert "missing_column" in result.output

    def test_verify_warnings_only_exits_0(self):
        mock = MockAsyncServices()
        issues = [Issue("servers", "warning", "stale_embedding_table", "stale table")]
        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.schema.verify_all", new_callable=AsyncMock, return_value=issues),
        ):
            result = runner.invoke(app, ["schema", "verify"])
        assert result.exit_code == 0
        assert "0 error(s), 1 warning(s)" in result.output


class TestRegisterCLI:
    def test_register_outputs_count(self):
        mock = MockAsyncServices()
        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.ontology.verify.register_models", new_callable=AsyncMock, return_value=13),
        ):
            result = runner.invoke(app, ["schema", "register"])
        assert result.exit_code == 0
        assert "Registered 13 model(s)" in result.output
