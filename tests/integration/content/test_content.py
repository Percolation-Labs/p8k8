"""Tests for ContentService — ingest, chunking, markdown upsert, structured upsert."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from p8.services.content import ContentService, IngestResult, load_structured
from p8.settings import Settings


# ============================================================================
# Helpers
# ============================================================================


def _make_content_service(
    *,
    s3_bucket: str = "",
    chunk_max: int = 1000,
    chunk_overlap: int = 200,
) -> tuple[ContentService, MagicMock, MagicMock]:
    """Create a ContentService with mocked DB and encryption."""
    db = MagicMock()
    db.fetch = AsyncMock(return_value=[])
    db.execute = AsyncMock()

    encryption = MagicMock()
    encryption.get_dek = AsyncMock(return_value=b"fake-key")
    encryption.encrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)
    encryption.decrypt_fields = MagicMock(side_effect=lambda cls, data, tid: data)

    file_service = MagicMock()
    file_service.read = AsyncMock(return_value=b"hello world")
    file_service.read_text = AsyncMock(return_value="# Hello\n\nWorld")
    file_service.write_to_bucket = AsyncMock(return_value="s3://bucket/key")

    settings = MagicMock(spec=Settings)
    settings.s3_bucket = s3_bucket
    settings.content_chunk_max_chars = chunk_max
    settings.content_chunk_overlap = chunk_overlap
    settings.openai_api_key = "test-key"
    settings.audio_chunk_duration_ms = 30000
    settings.audio_silence_thresh = -40
    settings.audio_min_silence_len = 700

    svc = ContentService(
        db=db, encryption=encryption, file_service=file_service, settings=settings
    )
    return svc, db, file_service


@dataclass
class _FakeChunk:
    content: str


@dataclass
class _FakeExtractResult:
    content: str
    chunks: list[_FakeChunk]


# ============================================================================
# load_structured tests
# ============================================================================


class TestLoadStructured:
    def test_json_list(self):
        result = load_structured('[{"name": "a"}]', "data.json")
        assert result == [{"name": "a"}]

    def test_json_dict_wrapped(self):
        result = load_structured('{"name": "a"}', "data.json")
        assert result == [{"name": "a"}]

    def test_yaml(self):
        result = load_structured("- name: a\n  kind: model\n", "data.yaml")
        assert result == [{"name": "a", "kind": "model"}]

    def test_unsupported_extension(self):
        with pytest.raises(ValueError, match="Unsupported"):
            load_structured("data", "data.txt")

    def test_invalid_top_level(self):
        with pytest.raises(ValueError, match="Expected list or dict"):
            load_structured('"just a string"', "data.json")


# ============================================================================
# ContentService.ingest tests
# ============================================================================


def _mock_create_moment_session(**overrides):
    """Return a patched create_moment_session that returns mock Moment+Session."""
    from p8.ontology.types import Moment, Session

    captured_kwargs = []

    async def _mock_cms(**kwargs):
        captured_kwargs.append(kwargs)
        name = kwargs.get("name", "mock-moment")
        moment_type = kwargs.get("moment_type", "content_upload")
        summary = kwargs.get("summary", "")
        moment = Moment(name=name, moment_type=moment_type, summary=summary)
        session = Session(name=name, mode=moment_type)
        return moment, session

    return _mock_cms, captured_kwargs


class TestIngest:
    @pytest.mark.asyncio
    async def test_basic_ingest(self):
        """ingest() extracts text, creates File + Resource entities + session."""
        svc, db, _ = _make_content_service()

        fake_result = _FakeExtractResult(
            content="Full extracted text from the document.",
            chunks=[_FakeChunk("Chunk one."), _FakeChunk("Chunk two."), _FakeChunk("Chunk three.")],
        )

        from p8.ontology.types import File, Resource

        mock_file = File(name="test.pdf", parsed_content="Full extracted text from the document.")
        mock_resources = [
            Resource(name=f"test-chunk-{i:04d}", ordinal=i, content=c.content)
            for i, c in enumerate(fake_result.chunks)
        ]

        async def mock_upsert(entities):
            if isinstance(entities, list):
                return mock_resources[: len(entities)]
            return [mock_file]

        mock_cms, cms_kwargs = _mock_create_moment_session()

        with (
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(
                b"pdf bytes",
                "test.pdf",
                mime_type="application/pdf",
            )

        assert isinstance(result, IngestResult)
        assert result.chunk_count == 3
        assert result.file.name == "test.pdf"
        assert len(result.resources) == 3
        assert result.session_id is not None

    @pytest.mark.asyncio
    async def test_small_file_single_chunk(self):
        """When Kreuzberg returns no chunks, full text becomes single chunk."""
        svc, db, _ = _make_content_service()

        fake_result = _FakeExtractResult(content="Small text.", chunks=[])

        from p8.ontology.types import File, Resource

        mock_file = File(name="small.txt", parsed_content="Small text.")
        mock_resource = Resource(name="small-chunk-0000", ordinal=0, content="Small text.")

        call_count = 0

        async def mock_upsert(entities):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_file]
            if isinstance(entities, list):
                return [mock_resource]
            return [mock_file]

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(b"small", "small.txt")

        assert result.chunk_count == 1
        assert result.total_chars == 11

    @pytest.mark.asyncio
    async def test_s3_upload(self):
        """When s3_key and bucket configured, uploads before extraction."""
        svc, db, file_service = _make_content_service(s3_bucket="my-bucket")

        fake_result = _FakeExtractResult(content="Text.", chunks=[])

        from p8.ontology.types import File, Resource

        mock_file = File(name="doc.pdf", uri="s3://my-bucket/doc.pdf")
        mock_resource = Resource(name="doc-chunk-0000", ordinal=0, content="Text.")

        call_count = 0

        async def mock_upsert(entities):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_file]
            if isinstance(entities, list):
                return [mock_resource]
            return [mock_file]

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(
                b"pdf", "doc.pdf", s3_key="doc.pdf"
            )

        file_service.write_to_bucket.assert_awaited_once_with("doc.pdf", b"pdf")

    @pytest.mark.asyncio
    async def test_graph_edges_and_metadata(self):
        """Resource entities get chunk_of graph_edge and file_id metadata."""
        svc, db, _ = _make_content_service()

        fake_result = _FakeExtractResult(content="Text.", chunks=[_FakeChunk("Chunk.")])

        from p8.ontology.types import File, Resource

        mock_file = File(name="report.pdf", parsed_content="Text.")
        captured_resources = []

        async def mock_upsert(entities):
            if isinstance(entities, list):
                captured_resources.extend(entities)
                return [Resource(name=e.name, ordinal=e.ordinal, content=e.content) for e in entities]
            return [mock_file]

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            await svc.ingest(b"pdf", "report.pdf")

        assert len(captured_resources) == 1
        r = captured_resources[0]
        assert r.graph_edges == [{"target": "report", "relation": "chunk_of"}]
        assert r.metadata["source_filename"] == "report.pdf"
        assert "file_id" in r.metadata

    @pytest.mark.asyncio
    async def test_ingest_creates_moment_session(self):
        """ingest() calls create_moment_session with correct metadata."""
        svc, db, _ = _make_content_service()

        fake_result = _FakeExtractResult(content="Text.", chunks=[_FakeChunk("Chunk.")])

        from p8.ontology.types import File, Resource

        mock_file = File(name="notes.pdf", parsed_content="Text.")
        mock_resource = Resource(name="notes-chunk-0000", ordinal=0, content="Chunk.")

        async def mock_upsert(entities):
            if isinstance(entities, list):
                return [mock_resource]
            return [mock_file]

        mock_cms, cms_kwargs = _mock_create_moment_session()

        with (
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=fake_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(b"pdf", "notes.pdf")

        assert len(cms_kwargs) == 1
        kw = cms_kwargs[0]
        assert kw["name"] == "upload-notes"
        assert kw["moment_type"] == "content_upload"
        assert kw["metadata"]["file_name"] == "notes.pdf"
        assert "notes-chunk-0000" in kw["metadata"]["resource_keys"]
        assert kw["metadata"]["source"] == "upload"
        assert result.session_id is not None


# ============================================================================
# ContentService.ingest_path tests
# ============================================================================


class TestIngestPath:
    @pytest.mark.asyncio
    async def test_reads_file_and_delegates(self):
        """ingest_path reads bytes then calls ingest."""
        svc, _, file_service = _make_content_service()
        file_service.read = AsyncMock(return_value=b"file bytes")

        with patch.object(svc, "ingest", new_callable=AsyncMock) as mock_ingest:
            mock_ingest.return_value = IngestResult(
                file=MagicMock(), resources=[], chunk_count=0, total_chars=0
            )
            await svc.ingest_path("/tmp/test.pdf")

        file_service.read.assert_awaited_once_with("/tmp/test.pdf")
        mock_ingest.assert_awaited_once()
        call_args = mock_ingest.call_args
        assert call_args[0][0] == b"file bytes"
        assert call_args[0][1] == "test.pdf"


# ============================================================================
# ContentService.upsert_markdown tests
# ============================================================================


class TestUpsertMarkdown:
    @pytest.mark.asyncio
    async def test_upserts_ontologies(self):
        """upsert_markdown reads files and creates Ontology entities."""
        svc, db, file_service = _make_content_service()

        file_service.read_text = AsyncMock(return_value="# Architecture\n\nContent here.")

        from p8.ontology.types import Ontology

        mock_ontology = Ontology(name="arch", content="# Architecture\n\nContent here.")

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(return_value=[mock_ontology])
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_markdown(["/docs/arch.md"])

        assert result.count == 1
        assert result.table == "ontologies"


# ============================================================================
# ContentService.upsert_structured tests
# ============================================================================


class TestUpsertStructured:
    @pytest.mark.asyncio
    async def test_upserts_data(self):
        """upsert_structured validates and upserts items."""
        svc, db, _ = _make_content_service()

        from p8.ontology.types import Schema

        mock_schema = Schema(name="test", kind="agent")

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(return_value=[mock_schema])
            MockRepo.return_value = mock_repo_instance

            result = await svc.upsert_structured(
                [{"name": "test", "kind": "agent"}],
                Schema,
            )

        assert result.count == 1
        assert result.table == "schemas"

    @pytest.mark.asyncio
    async def test_stamps_tenant_and_user(self):
        """upsert_structured applies tenant_id and user_id defaults."""
        svc, db, _ = _make_content_service()

        from p8.ontology.types import Schema

        captured = []

        async def mock_upsert(entities):
            captured.extend(entities)
            return entities

        with patch("p8.services.content.Repository") as MockRepo:
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance

            from uuid import UUID
            test_uid = UUID("00000000-0000-0000-0000-000000000001")
            await svc.upsert_structured(
                [{"name": "test", "kind": "model"}],
                Schema,
                tenant_id="t1",
                user_id=test_uid,
            )

        assert captured[0].tenant_id == "t1"
        assert captured[0].user_id == UUID("00000000-0000-0000-0000-000000000001")


# ============================================================================
# Audio ingest tests
# ============================================================================


class TestAudioIngest:
    @pytest.mark.asyncio
    async def test_audio_ingest_splits_and_transcribes(self):
        """Audio ingest: splits audio, transcribes each chunk, creates Resources."""
        svc, db, _ = _make_content_service()

        from p8.ontology.types import File, Resource

        mock_file = File(name="interview", parsed_content="Hello world. Goodbye world.")
        mock_resources = [
            Resource(name="interview-chunk-0000", ordinal=0, content="Hello world. Goodbye world.")
        ]

        call_count = 0

        async def mock_upsert(entities):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [mock_file]
            if isinstance(entities, list):
                return mock_resources[: len(entities)]
            return [mock_file]

        # Mock pydub: AudioSegment and split_on_silence
        mock_segment = MagicMock()
        mock_segment.export = MagicMock(side_effect=lambda buf, format: buf.write(b"fake-wav"))
        mock_segment.__len__ = MagicMock(return_value=5000)  # 5s segment, above 500ms minimum

        mock_audio = MagicMock()
        mock_split = MagicMock(return_value=[mock_segment, mock_segment])

        # Mock httpx response
        mock_response = MagicMock()
        mock_response.text = "Hello world."
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # Mock Kreuzberg re-chunking
        rechunk_result = _FakeExtractResult(
            content="Hello world. Hello world.",
            chunks=[_FakeChunk("Hello world. Hello world.")],
        )

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("pydub.AudioSegment.from_file", return_value=mock_audio),
            patch("pydub.silence.split_on_silence", mock_split),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=rechunk_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(
                b"fake-audio-bytes",
                "interview.mp3",
                mime_type="audio/mpeg",
            )

        assert isinstance(result, IngestResult)
        assert result.file.name == "interview"
        # Whisper called twice (two segments)
        assert mock_client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_audio_ingest_fallback_to_fixed_chunks(self):
        """When split_on_silence returns <=1 segment, falls back to make_chunks."""
        svc, db, _ = _make_content_service()

        from p8.ontology.types import File

        mock_file = File(name="podcast", parsed_content="Transcribed text.")

        async def mock_upsert(entities):
            return [mock_file]

        mock_segment = MagicMock()
        mock_segment.export = MagicMock(side_effect=lambda buf, format: buf.write(b"fake-wav"))
        mock_segment.__len__ = MagicMock(return_value=5000)  # 5s segment, above 500ms minimum

        mock_audio = MagicMock()
        mock_split = MagicMock(return_value=[mock_segment])  # only 1 segment → fallback
        mock_make_chunks = MagicMock(return_value=[mock_segment, mock_segment, mock_segment])

        mock_response = MagicMock()
        mock_response.text = "Transcribed text."
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        rechunk_result = _FakeExtractResult(
            content="Transcribed text. Transcribed text. Transcribed text.",
            chunks=[_FakeChunk("Transcribed text. Transcribed text. Transcribed text.")],
        )

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("pydub.AudioSegment.from_file", return_value=mock_audio),
            patch("pydub.silence.split_on_silence", mock_split),
            patch("pydub.utils.make_chunks", mock_make_chunks),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("kreuzberg.extract_bytes", new_callable=AsyncMock, return_value=rechunk_result),
            patch("kreuzberg.ChunkingConfig"),
            patch("kreuzberg.ExtractionConfig"),
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(
                b"fake-audio",
                "podcast.mp3",
                mime_type="audio/mpeg",
            )

        mock_make_chunks.assert_called_once_with(mock_audio, 30000)
        assert mock_client.post.await_count == 3  # 3 fixed chunks

    @pytest.mark.asyncio
    async def test_audio_no_api_key_raises_error(self):
        """When openai_api_key is empty, audio ingest raises ContentProcessingError."""
        from p8.services.content import ContentProcessingError

        svc, db, _ = _make_content_service()
        svc.settings.openai_api_key = ""

        with pytest.raises(ContentProcessingError, match="OpenAI API key"):
            await svc.ingest(
                b"audio-data",
                "voice.wav",
                mime_type="audio/wav",
            )


# ============================================================================
# Image ingest tests
# ============================================================================


class TestImageIngest:
    @pytest.mark.asyncio
    async def test_image_creates_file_no_resources(self):
        """Image ingest creates File entity with empty parsed_content, no Resources."""
        svc, db, _ = _make_content_service()

        from p8.ontology.types import File

        mock_file = File(name="photo", parsed_content="")

        async def mock_upsert(entities):
            return [mock_file]

        mock_cms, _ = _mock_create_moment_session()

        with (
            patch("p8.services.content.Repository") as MockRepo,
            patch("p8.services.content.MemoryService") as MockMem,
        ):
            mock_repo_instance = MagicMock()
            mock_repo_instance.upsert = AsyncMock(side_effect=mock_upsert)
            MockRepo.return_value = mock_repo_instance
            MockMem.return_value.create_moment_session = AsyncMock(side_effect=mock_cms)

            result = await svc.ingest(
                b"image-bytes",
                "photo.jpg",
                mime_type="image/jpeg",
            )

        assert result.chunk_count == 0
        assert result.total_chars == 0
        assert result.resources == []
