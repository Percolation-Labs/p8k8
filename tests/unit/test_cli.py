"""Unit tests for CLI commands — query, upsert, chat (all mocked, no DB)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from p8.api.cli import app

runner = CliRunner()


# ============================================================================
# Helpers
# ============================================================================


def _mock_services():
    """Create mock services tuple matching async_services() yield (6-tuple)."""
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db.rem_query = AsyncMock(return_value=[])
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()

    encryption = MagicMock()
    encryption.ensure_system_key = AsyncMock()
    encryption.get_dek = AsyncMock(return_value=b"fake-key")
    encryption.encrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)
    encryption.decrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)

    settings = MagicMock()
    settings.system_tenant_id = "__system__"
    settings.s3_bucket = ""
    settings.content_chunk_max_chars = 1000
    settings.content_chunk_overlap = 200

    file_service = MagicMock()
    file_service.read_text = AsyncMock(return_value="")
    file_service.list_dir = MagicMock(return_value=[])

    content_service = MagicMock()
    content_service.ingest_path = AsyncMock()
    content_service.ingest_directory = AsyncMock(return_value=[])
    content_service.upsert_markdown = AsyncMock()
    content_service.upsert_structured = AsyncMock()

    embedding_service = None
    return db, encryption, settings, file_service, content_service, embedding_service


class _MockAsyncServices:
    """Async context manager that yields mock services."""

    def __init__(self):
        self.services = _mock_services()

    async def __aenter__(self):
        return self.services

    async def __aexit__(self, *args):
        pass


# ============================================================================
# Query tests
# ============================================================================


class TestQueryCLI:
    def test_query_one_shot_lookup(self):
        mock = _MockAsyncServices()
        mock.services[0].rem_query = AsyncMock(
            return_value=[{"entity_type": "schemas", "data": {"key": "test", "id": "abc"}}]
        )
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            result = runner.invoke(app, ["query", 'LOOKUP "test"'])
        assert result.exit_code == 0
        assert "test" in result.output

    def test_query_one_shot_fuzzy(self):
        mock = _MockAsyncServices()
        mock.services[0].rem_query = AsyncMock(
            return_value=[{"entity_type": "schemas", "similarity_score": 0.8, "data": {"key": "fuzzy-match"}}]
        )
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            result = runner.invoke(app, ["query", 'FUZZY "test"'])
        assert result.exit_code == 0

    def test_query_one_shot_sql(self):
        mock = _MockAsyncServices()
        mock.services[0].rem_query = AsyncMock(
            return_value=[{"name": "agent-1"}, {"name": "agent-2"}]
        )
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            result = runner.invoke(app, ["query", "SELECT name FROM schemas LIMIT 3"])
        assert result.exit_code == 0
        assert "agent-1" in result.output

    def test_query_table_format(self):
        mock = _MockAsyncServices()
        mock.services[0].rem_query = AsyncMock(
            return_value=[{"entity_type": "schemas", "data": {"key": "test-key", "type": "schemas"}}]
        )
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            result = runner.invoke(app, ["query", "--format", "table", 'LOOKUP "test"'])
        assert result.exit_code == 0
        assert "entity_type" in result.output

    def test_query_error(self):
        mock = _MockAsyncServices()
        mock.services[0].rem_query = AsyncMock(side_effect=ValueError("Blocked SQL keyword: DROP"))
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            result = runner.invoke(app, ["query", "DROP TABLE schemas"])
        assert result.exit_code == 1
        assert "Error" in result.output


# ============================================================================
# Upsert tests
# ============================================================================


class TestUpsertCLI:
    def test_upsert_json(self):
        from p8.services.content import BulkUpsertResult

        mock = _MockAsyncServices()
        data = [{"name": "test-schema", "kind": "model"}]
        mock.services[3].read_text = AsyncMock(return_value=json.dumps(data))
        mock.services[4].upsert_structured = AsyncMock(
            return_value=BulkUpsertResult(count=1, table="schemas")
        )

        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
                json.dump(data, f)
                f.flush()
                result = runner.invoke(app, ["upsert", "schemas", f.name])
                os.unlink(f.name)
        assert result.exit_code == 0
        assert "Upserted 1 rows into schemas" in result.output

    def test_upsert_yaml(self):
        from p8.services.content import BulkUpsertResult

        mock = _MockAsyncServices()
        yaml_text = "- name: test-schema\n  kind: model\n"
        mock.services[3].read_text = AsyncMock(return_value=yaml_text)
        mock.services[4].upsert_structured = AsyncMock(
            return_value=BulkUpsertResult(count=1, table="schemas")
        )

        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
                f.write(yaml_text)
                f.flush()
                result = runner.invoke(app, ["upsert", "schemas", f.name])
                os.unlink(f.name)
        assert result.exit_code == 0
        assert "Upserted 1 rows into schemas" in result.output

    def test_upsert_markdown_file(self):
        from p8.services.content import BulkUpsertResult

        mock = _MockAsyncServices()
        mock.services[4].upsert_markdown = AsyncMock(
            return_value=BulkUpsertResult(count=1, table="ontologies")
        )

        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
                f.write("# Architecture\n\nSome content here.")
                f.flush()
                result = runner.invoke(app, ["upsert", f.name])
                os.unlink(f.name)
        assert result.exit_code == 0
        assert "Upserted 1 rows into ontologies" in result.output

    def test_upsert_markdown_dir(self):
        from p8.services.content import BulkUpsertResult

        mock = _MockAsyncServices()

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ["one.md", "two.md"]:
                Path(tmpdir, name).write_text(f"# {name}\n\nContent of {name}.")
            mock.services[3].list_dir = MagicMock(
                return_value=[str(Path(tmpdir, "one.md")), str(Path(tmpdir, "two.md"))]
            )
            mock.services[4].upsert_markdown = AsyncMock(
                return_value=BulkUpsertResult(count=2, table="ontologies")
            )

            with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
                result = runner.invoke(app, ["upsert", tmpdir])
        assert result.exit_code == 0
        assert "Upserted 2 rows into ontologies" in result.output

    def test_upsert_json_requires_table(self):
        """JSON/YAML without table arg should fail."""
        mock = _MockAsyncServices()
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
                json.dump([{"name": "test"}], f)
                f.flush()
                result = runner.invoke(app, ["upsert", f.name])
                os.unlink(f.name)
        assert result.exit_code == 1
        assert "require a table name" in result.output

    def test_upsert_resources_calls_content_service(self):
        """Resources + file should delegate to ContentService.ingest_path()."""
        from p8.services.content import IngestResult

        mock = _MockAsyncServices()
        mock_file = MagicMock()
        mock_file.name = "test.md"
        mock_file.id = "00000000-0000-0000-0000-000000000001"
        mock.services[4].ingest_path = AsyncMock(
            return_value=IngestResult(
                file=mock_file, resources=[], chunk_count=2, total_chars=100
            )
        )
        with patch("p8.services.bootstrap.bootstrap_services", return_value=mock):
            with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
                f.write("# Test")
                f.flush()
                result = runner.invoke(app, ["upsert", "resources", f.name])
                os.unlink(f.name)
        assert result.exit_code == 0
        assert "chunks" in result.output


# ============================================================================
# Chat tests
# ============================================================================


class TestChatCLI:
    def test_chat_new_session(self):
        from uuid import UUID

        from p8.api.controllers.chat import ChatContext, ChatTurn

        mock = _MockAsyncServices()

        mock_adapter = MagicMock()
        mock_adapter.schema.name = "general"

        mock_ctx = ChatContext(
            adapter=mock_adapter,
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            agent=MagicMock(),
            injector=MagicMock(),
            message_history=[],
        )
        mock_turn = ChatTurn(assistant_text="Hello!")

        mock_controller_cls = MagicMock()
        mock_controller = MagicMock()
        mock_controller.resolve_agent = AsyncMock(return_value=mock_adapter)
        mock_controller.get_or_create_session = AsyncMock(
            return_value=(mock_ctx.session_id, MagicMock())
        )
        mock_controller.prepare = AsyncMock(return_value=mock_ctx)
        mock_controller.run_turn = AsyncMock(return_value=mock_turn)
        mock_controller_cls.return_value = mock_controller

        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.chat.ChatController", mock_controller_cls),
        ):
            result = runner.invoke(app, ["chat"], input="hello\nexit\n")
        assert result.exit_code == 0
        assert "New session" in result.output
        assert "Hello!" in result.output

    def test_chat_with_agent(self):
        from uuid import UUID

        from p8.api.controllers.chat import ChatContext, ChatTurn

        mock = _MockAsyncServices()

        mock_adapter = MagicMock()
        mock_adapter.schema.name = "query-agent"

        mock_ctx = ChatContext(
            adapter=mock_adapter,
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            agent=MagicMock(),
            injector=MagicMock(),
            message_history=[],
        )

        mock_controller_cls = MagicMock()
        mock_controller = MagicMock()
        mock_controller.resolve_agent = AsyncMock(return_value=mock_adapter)
        mock_controller.get_or_create_session = AsyncMock(
            return_value=(mock_ctx.session_id, MagicMock())
        )
        mock_controller.prepare = AsyncMock(return_value=mock_ctx)
        mock_controller.run_turn = AsyncMock(
            return_value=ChatTurn(assistant_text="Agent response"),
        )
        mock_controller_cls.return_value = mock_controller

        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.chat.ChatController", mock_controller_cls),
        ):
            result = runner.invoke(app, ["chat", "--agent", "query-agent"], input="exit\n")
        assert result.exit_code == 0
        assert "query-agent" in result.output

    def test_chat_agent_not_found(self):
        mock = _MockAsyncServices()

        mock_controller_cls = MagicMock()
        mock_controller = MagicMock()
        mock_controller.resolve_agent = AsyncMock(side_effect=ValueError("not found"))
        mock_controller_cls.return_value = mock_controller

        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.chat.ChatController", mock_controller_cls),
        ):
            result = runner.invoke(app, ["chat", "--agent", "nope"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_chat_with_delegation(self):
        """ChatController.run_turn works when agent delegates via ask_agent."""
        from uuid import UUID

        from p8.api.controllers.chat import ChatContext, ChatTurn

        mock = _MockAsyncServices()

        mock_adapter = MagicMock()
        mock_adapter.schema.name = "parent-agent"

        mock_ctx = ChatContext(
            adapter=mock_adapter,
            session_id=UUID("00000000-0000-0000-0000-000000000003"),
            agent=MagicMock(),
            injector=MagicMock(),
            message_history=[],
        )
        mock_turn = ChatTurn(
            assistant_text="The child agent said: answer is 42",
        )

        mock_controller_cls = MagicMock()
        mock_controller = MagicMock()
        mock_controller.resolve_agent = AsyncMock(return_value=mock_adapter)
        mock_controller.get_or_create_session = AsyncMock(
            return_value=(mock_ctx.session_id, MagicMock())
        )
        mock_controller.prepare = AsyncMock(return_value=mock_ctx)
        mock_controller.run_turn = AsyncMock(return_value=mock_turn)
        mock_controller_cls.return_value = mock_controller

        with (
            patch("p8.services.bootstrap.bootstrap_services", return_value=mock),
            patch("p8.api.cli.chat.ChatController", mock_controller_cls),
        ):
            result = runner.invoke(
                app, ["chat", "--agent", "parent-agent"], input="delegate to child\nexit\n",
            )
        assert result.exit_code == 0
        assert "answer is 42" in result.output
