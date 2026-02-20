"""Content ingestion — file upload → extract text → chunk → persist.

Pipeline: bytes → Kreuzberg extract → File entity (full text) + Resource entities (chunks).
Encryption and embedding happen automatically via Repository and DB triggers.

Also provides bulk upsert helpers for markdown → ontologies and structured data → any table,
keeping CLI/API code thin.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID

from p8.ontology.base import CoreModel
from p8.ontology.types import File, Moment, Ontology, Resource
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.files import FileService
from p8.services.repository import Repository
from p8.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Return value from ContentService.ingest()."""

    file: File
    resources: list[Resource]
    chunk_count: int
    total_chars: int
    session_id: UUID | None = None


@dataclass
class BulkUpsertResult:
    """Return value from bulk_upsert_*() helpers."""

    count: int
    table: str


@dataclass
class ContentService:
    """Extract, chunk, and persist content from uploaded files.

    Entry points:
      ingest()            — from raw bytes (API upload)
      ingest_path()       — from a local/S3 path (CLI)
      ingest_directory()  — from a directory of files (CLI)
      upsert_markdown()   — markdown files → ontologies (or any table)
      upsert_structured() — JSON/YAML data → any table
    """

    db: Database
    encryption: EncryptionService
    file_service: FileService
    settings: Settings

    # ── Content Ingestion (resources) ─────────────────────────────────────

    async def ingest(
        self,
        data: bytes,
        filename: str,
        *,
        mime_type: str | None = None,
        s3_key: str | None = None,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        session_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        max_chars: int | None = None,
        overlap: int | None = None,
    ) -> IngestResult:
        """Ingest raw bytes: extract text, chunk, persist File + Resources."""
        from kreuzberg import ChunkingConfig, ExtractionConfig, extract_bytes

        if not mime_type:
            mime_type = FileService.mime_type_from_path(filename)

        # Upload to S3 if configured
        uri: str | None = None
        if s3_key and self.settings.s3_bucket:
            uri = await self.file_service.write_to_bucket(s3_key, data)

        # Route by MIME type: audio/image get special processing, rest use Kreuzberg
        chunk_max = max_chars or self.settings.content_chunk_max_chars
        chunk_overlap = overlap or self.settings.content_chunk_overlap

        if mime_type and mime_type.startswith("audio/"):
            full_text, chunk_texts = await self._process_audio(
                data, mime_type, chunk_max, chunk_overlap
            )
        elif mime_type and mime_type.startswith("image/"):
            full_text, chunk_texts = await self._process_image(data, mime_type)
        else:
            # Existing Kreuzberg path for documents/text
            chunking = ChunkingConfig(max_chars=chunk_max, max_overlap=chunk_overlap)
            config = ExtractionConfig(chunking=chunking)

            result = await extract_bytes(data, mime_type=mime_type, config=config)

            full_text = result.content
            # Kreuzberg returns Chunk objects — extract text content
            chunk_texts = [c.content for c in result.chunks] if result.chunks else []

            # If no chunks returned (small file), use full text as single chunk
            if not chunk_texts and full_text:
                chunk_texts = [full_text]

        stem = Path(filename).stem

        # Persist File entity
        file_repo = Repository(File, self.db, self.encryption)
        file_entity = File(
            name=stem,
            uri=uri,
            mime_type=mime_type,
            size_bytes=len(data),
            parsed_content=full_text,
            tenant_id=tenant_id,
            user_id=user_id,
            tags=list(tags or []),
        )
        [file_entity] = await file_repo.upsert(file_entity)

        # Persist Resource entities (one per chunk)
        resource_entities: list[Resource] = []
        if chunk_texts:
            resources_to_upsert = []
            for i, chunk_text in enumerate(chunk_texts):
                resource = Resource(
                    name=f"{stem}-chunk-{i:04d}",
                    uri=uri,
                    ordinal=i,
                    content=chunk_text,
                    category=category,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    tags=list(tags or []),
                    graph_edges=[{"target": stem, "relation": "chunk_of"}],
                    metadata={"file_id": str(file_entity.id), "source_filename": filename},
                )
                resources_to_upsert.append(resource)

            resource_repo = Repository(Resource, self.db, self.encryption)
            resource_entities = await resource_repo.upsert(resources_to_upsert)

        # Create content_upload moment (session-scoped when session_id provided)
        moment = Moment(
            name=f"upload-{stem}",
            moment_type="content_upload",
            summary=f"Uploaded {filename} ({len(chunk_texts)} chunks, {len(full_text) if full_text else 0} chars)",
            source_session_id=session_id,
            metadata={
                "file_id": str(file_entity.id),
                "resource_keys": [r.name for r in resource_entities],
                "source": "upload",
                "chunk_count": len(chunk_texts),
            },
            tenant_id=tenant_id,
            user_id=user_id,
        )
        moment_repo = Repository(Moment, self.db, self.encryption)
        [moment] = await moment_repo.upsert(moment)

        # Create a session for this upload when no session_id was provided.
        # When session_id is given (e.g. chat-initiated upload), the moment
        # is already linked to that session via source_session_id.
        from p8.ontology.types import Session

        result_session_id: UUID | None = UUID(session_id) if session_id else None

        if not session_id:
            session = Session(
                name=f"upload: {filename}",
                agent_name="content-upload",
                mode="upload",
                user_id=user_id,
                tenant_id=tenant_id,
                metadata={
                    "resource_keys": [r.name for r in resource_entities],
                    "moment_id": str(moment.id),
                    "source": filename,
                },
            )
            session_repo = Repository(Session, self.db, self.encryption)
            [session] = await session_repo.upsert(session)
            result_session_id = session.id

            # Link moment to the new session
            await self.db.execute(
                "UPDATE moments SET source_session_id = $1 WHERE id = $2",
                session.id,
                moment.id,
            )

        return IngestResult(
            file=file_entity,
            resources=resource_entities,
            chunk_count=len(chunk_texts),
            total_chars=len(full_text) if full_text else 0,
            session_id=result_session_id,
        )

    # ── Audio / Image processors ──────────────────────────────────────────

    async def _process_audio(
        self,
        data: bytes,
        mime_type: str,
        chunk_max: int,
        chunk_overlap: int,
    ) -> tuple[str, list[str]]:
        """Transcribe audio via OpenAI Whisper API, then chunk the transcript text."""
        import httpx
        from pydub import AudioSegment
        from pydub.silence import split_on_silence
        from pydub.utils import make_chunks

        if not self.settings.openai_api_key:
            logger.warning("No openai_api_key configured — skipping audio transcription")
            return ("", [])

        # Derive pydub format from MIME type (audio/mpeg → mp3, audio/wav → wav, etc.)
        fmt = mime_type.split("/")[-1]
        if fmt == "mpeg":
            fmt = "mp3"

        audio = AudioSegment.from_file(BytesIO(data), format=fmt)

        # Split on silence; fall back to fixed-size chunks
        segments = split_on_silence(
            audio,
            min_silence_len=self.settings.audio_min_silence_len,
            silence_thresh=self.settings.audio_silence_thresh,
        )
        if len(segments) <= 1:
            segments = make_chunks(audio, self.settings.audio_chunk_duration_ms)

        # Transcribe each segment via OpenAI Whisper API
        transcriptions: list[str] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for segment in segments:
                buf = BytesIO()
                segment.export(buf, format="wav")
                buf.seek(0)

                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                    data={"model": "whisper-1", "response_format": "text"},
                    files={"file": ("chunk.wav", buf, "audio/wav")},
                )
                resp.raise_for_status()
                transcriptions.append(resp.text.strip())

        full_text = " ".join(transcriptions)

        # Re-chunk the transcript text via Kreuzberg for consistent sizes
        from kreuzberg import ChunkingConfig, ExtractionConfig, extract_bytes

        chunking = ChunkingConfig(max_chars=chunk_max, max_overlap=chunk_overlap)
        config = ExtractionConfig(chunking=chunking)
        result = await extract_bytes(full_text.encode(), mime_type="text/plain", config=config)
        chunk_texts = [c.content for c in result.chunks] if result.chunks else []
        if not chunk_texts and full_text:
            chunk_texts = [full_text]

        return (full_text, chunk_texts)

    async def _process_image(
        self, data: bytes, mime_type: str
    ) -> tuple[str, list[str]]:
        """Placeholder for image processing — File entity created, no text extraction yet."""
        # TODO: LLM vision API to describe image
        return ("", [])

    # ── Path / Directory ingestion ─────────────────────────────────────────

    async def ingest_path(
        self,
        path: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        upload_to_s3: bool = False,
    ) -> IngestResult:
        """Ingest from a local/S3 path. Reads bytes, then delegates to ingest()."""
        data = await self.file_service.read(path)
        filename = Path(path).name if not path.startswith("s3://") else path.rsplit("/", 1)[-1]
        s3_key = filename if upload_to_s3 else None

        return await self.ingest(
            data,
            filename,
            tenant_id=tenant_id,
            user_id=user_id,
            category=category,
            tags=tags or [],
            s3_key=s3_key,
        )

    async def ingest_directory(
        self,
        dir_path: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        category: str | None = None,
    ) -> list[IngestResult]:
        """Ingest all files in a directory. Returns one IngestResult per file."""
        p = Path(dir_path)
        files = sorted(str(f) for f in p.rglob("*") if f.is_file())
        results = []
        for fp in files:
            result = await self.ingest_path(
                fp, tenant_id=tenant_id, user_id=user_id, category=category
            )
            results.append(result)
        return results

    # ── Markdown → Ontologies ─────────────────────────────────────────────

    async def upsert_markdown(
        self,
        paths: list[str],
        *,
        model_class: type[CoreModel] | None = None,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> BulkUpsertResult:
        """Read markdown files and upsert as entities (default: ontologies).

        Each file becomes one entity: name=stem, content=body.
        """
        cls = model_class or Ontology
        entities = []
        for fp in paths:
            text = await self.file_service.read_text(fp)
            stem = Path(fp).stem
            entity = cls.model_validate({
                "name": stem,
                "content": text,
                **({"tenant_id": tenant_id} if tenant_id else {}),
                **({"user_id": user_id} if user_id else {}),
            })
            entities.append(entity)

        repo = Repository(cls, self.db, self.encryption)
        results = await repo.upsert(entities)
        return BulkUpsertResult(count=len(results), table=cls.__table_name__)

    # ── Structured Data → Any Table ───────────────────────────────────────

    async def upsert_structured(
        self,
        items: list[dict],
        model_class: type[CoreModel],
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> BulkUpsertResult:
        """Validate and upsert structured data (from JSON/YAML) into a table."""
        entities = []
        for item in items:
            if tenant_id:
                item.setdefault("tenant_id", tenant_id)
            if user_id:
                item.setdefault("user_id", user_id)
            entity = model_class.model_validate(item)
            entities.append(entity)

        repo = Repository(model_class, self.db, self.encryption)
        results = await repo.upsert(entities)
        return BulkUpsertResult(count=len(results), table=model_class.__table_name__)


def load_structured(text: str, path: str) -> list[dict]:
    """Parse JSON or YAML text into a list of dicts. Utility for CLI/API."""
    ext = Path(path).suffix.lower()
    if ext in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
    elif ext == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"Unsupported structured format: {ext} (expected .json/.yaml/.yml)")

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(f"Expected list or dict at top level, got {type(data).__name__}")
    return data
