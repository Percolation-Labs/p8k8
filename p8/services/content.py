"""Content ingestion — file upload, text extraction, chunking, persistence.

Pipeline: bytes → Kreuzberg extract → File entity (full text) + Resource entities (chunks).
Encryption and embedding happen automatically via Repository and DB triggers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import UUID

from p8.ontology.base import CoreModel
from p8.ontology.types import File, Moment, Ontology, Resource, Session
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
    """Return value from bulk upsert helpers."""

    count: int
    table: str


@dataclass
class ContentService:
    """Extract, chunk, and persist content from uploaded files."""

    db: Database
    encryption: EncryptionService
    file_service: FileService
    settings: Settings

    @staticmethod
    def s3_key_for(filename: str, *, user_id: UUID | None = None) -> str:
        """Build a user-scoped, date-partitioned S3 key.

        Format: ``{user_id}/{YYYY}/{MM}/{DD}/{filename}``
        """
        now = datetime.now(timezone.utc)
        prefix = str(user_id) if user_id else "_anonymous_"
        return f"{prefix}/{now:%Y}/{now:%m}/{now:%d}/{filename}"

    # ── Public entry points ──────────────────────────────────────────────

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
        """Ingest raw bytes: upload to S3, extract text, chunk, persist."""
        mime_type = mime_type or FileService.mime_type_from_path(filename)
        tag_list = list(tags or [])

        uri = await self._upload_to_s3(data, filename, s3_key=s3_key, user_id=user_id)
        full_text, chunk_texts = await self._extract_text(
            data, mime_type, max_chars=max_chars, overlap=overlap,
        )

        stem = Path(filename).stem
        file_entity = await self._persist_file(
            stem, uri, mime_type, data, full_text,
            tenant_id=tenant_id, user_id=user_id, tags=tag_list,
        )

        # Generate and upload thumbnail for images
        thumb_data: bytes | None = None
        if mime_type and mime_type.startswith("image/"):
            thumb_data = await self._generate_thumbnail(data)
            if thumb_data and self.settings.s3_bucket and uri and uri.startswith("s3://"):
                _, orig_key = FileService._parse_s3_uri(uri)
                thumb_key = f"{orig_key}-thumb.jpg"
                thumb_uri = await self.file_service.write_to_bucket(thumb_key, thumb_data)
                file_entity.thumbnail_uri = thumb_uri
                repo = Repository(File, self.db, self.encryption)
                await repo.upsert(file_entity)

        resource_entities = await self._persist_chunks(
            stem, uri, chunk_texts, file_entity.id, filename,
            category=category, tenant_id=tenant_id, user_id=user_id, tags=tag_list,
        )
        moment = await self._create_upload_moment(
            stem, filename, file_entity, resource_entities, full_text,
            thumb_data=thumb_data,
            session_id=session_id, tenant_id=tenant_id, user_id=user_id,
        )
        result_session_id = await self._ensure_upload_session(
            filename, moment, resource_entities,
            session_id=session_id, tenant_id=tenant_id, user_id=user_id,
        )

        return IngestResult(
            file=file_entity,
            resources=resource_entities,
            chunk_count=len(chunk_texts),
            total_chars=len(full_text) if full_text else 0,
            session_id=result_session_id,
        )

    # ── Ingest sub-steps ─────────────────────────────────────────────────

    async def _upload_to_s3(
        self, data: bytes, filename: str, *, s3_key: str | None, user_id: UUID | None,
    ) -> str | None:
        """Upload to S3 if a bucket is configured. Returns the s3:// URI or None."""
        if not self.settings.s3_bucket:
            return None
        # TODO: async queue processing — return immediately and process via worker
        key = s3_key or self.s3_key_for(filename, user_id=user_id)
        return await self.file_service.write_to_bucket(key, data)

    async def _extract_text(
        self, data: bytes, mime_type: str, *, max_chars: int | None, overlap: int | None,
    ) -> tuple[str, list[str]]:
        """Route by MIME type and return (full_text, chunk_texts)."""
        chunk_max = max_chars or self.settings.content_chunk_max_chars
        chunk_overlap = overlap or self.settings.content_chunk_overlap

        if mime_type.startswith("audio/"):
            return await self._process_audio(data, mime_type, chunk_max, chunk_overlap)
        if mime_type.startswith("image/"):
            return await self._process_image(data, mime_type)
        return await self._process_document(data, mime_type, chunk_max, chunk_overlap)

    async def _process_document(
        self, data: bytes, mime_type: str, chunk_max: int, chunk_overlap: int,
    ) -> tuple[str, list[str]]:
        """Extract and chunk text from documents via Kreuzberg.

        Uses subprocess workaround when running in a daemon process (e.g. under
        Hypercorn/Uvicorn) because Kreuzberg's ProcessPoolExecutor cannot fork
        from daemon processes.
        """
        import multiprocessing
        is_daemon = False
        try:
            is_daemon = multiprocessing.current_process().daemon
        except Exception:
            pass

        if is_daemon:
            return await self._process_document_subprocess(
                data, mime_type, chunk_max, chunk_overlap,
            )

        from kreuzberg import ChunkingConfig, ExtractionConfig, extract_bytes

        chunking = ChunkingConfig(max_chars=chunk_max, max_overlap=chunk_overlap)
        result = await extract_bytes(
            data, mime_type=mime_type, config=ExtractionConfig(chunking=chunking),
        )
        full_text = result.content
        chunks = [c.content for c in result.chunks] if result.chunks else []
        if not chunks and full_text:
            chunks = [full_text]
        return full_text, chunks

    async def _process_document_subprocess(
        self, data: bytes, mime_type: str, chunk_max: int, chunk_overlap: int,
    ) -> tuple[str, list[str]]:
        """Run Kreuzberg in a separate subprocess to bypass daemon restrictions."""
        import asyncio
        import subprocess
        import sys
        import tempfile

        # Write data to temp file for the subprocess
        suffix = self._extension_for_mime(mime_type)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        script = f"""
import json, sys
from pathlib import Path
from kreuzberg import ChunkingConfig, ExtractionConfig, extract_file_sync

chunking = ChunkingConfig(max_chars={chunk_max}, max_overlap={chunk_overlap})
config = ExtractionConfig(chunking=chunking)
result = extract_file_sync(Path(sys.argv[1]), config=config)

chunks = [c.content for c in result.chunks] if result.chunks else []
if not chunks and result.content:
    chunks = [result.content]

output = {{"content": result.content, "chunks": chunks}}
print(json.dumps(output))
"""
        try:
            loop = asyncio.get_event_loop()
            proc_result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-c", script, tmp_path],
                    capture_output=True, text=True, timeout=300,
                ),
            )
            if proc_result.returncode != 0:
                logger.error("Kreuzberg subprocess failed: %s", proc_result.stderr)
                raise RuntimeError(f"Document extraction failed: {proc_result.stderr[-500:]}")

            import json as _json
            parsed = _json.loads(proc_result.stdout)
            return parsed["content"], parsed["chunks"]
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @staticmethod
    def _extension_for_mime(mime_type: str) -> str:
        """Map common MIME types to file extensions for temp files."""
        _map = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "text/plain": ".txt",
            "text/markdown": ".md",
            "text/html": ".html",
        }
        return _map.get(mime_type, ".bin")

    async def _persist_file(
        self, stem: str, uri: str | None, mime_type: str, data: bytes, full_text: str,
        *, tenant_id: str | None, user_id: UUID | None, tags: list[str],
    ) -> File:
        """Create the File entity."""
        repo = Repository(File, self.db, self.encryption)
        entity = File(
            name=stem, uri=uri, mime_type=mime_type,
            size_bytes=len(data), parsed_content=full_text,
            tenant_id=tenant_id, user_id=user_id, tags=tags,
        )
        [entity] = await repo.upsert(entity)
        return entity

    async def _persist_chunks(
        self,
        stem: str, uri: str | None, chunk_texts: list[str],
        file_id: UUID, filename: str,
        *, category: str | None, tenant_id: str | None, user_id: UUID | None, tags: list[str],
    ) -> list[Resource]:
        """Create one Resource entity per text chunk."""
        if not chunk_texts:
            return []
        resources = [
            Resource(
                name=f"{stem}-chunk-{i:04d}", ordinal=i,
                content=text, category=category,
                tenant_id=tenant_id, user_id=user_id, tags=tags,
                graph_edges=[{"target": stem, "relation": "chunk_of"}],
                metadata={"file_id": str(file_id), "source_filename": filename, "source_uri": uri},
            )
            for i, text in enumerate(chunk_texts)
        ]
        repo = Repository(Resource, self.db, self.encryption)
        return await repo.upsert(resources)

    async def _create_upload_moment(
        self,
        stem: str, filename: str,
        file_entity: File, resources: list[Resource], full_text: str,
        *,
        thumb_data: bytes | None = None,
        session_id: str | None, tenant_id: str | None, user_id: UUID | None,
    ) -> Moment:
        """Record a content_upload moment with content preview and resource keys."""
        import base64

        char_count = len(full_text) if full_text else 0
        resource_keys = [r.name for r in resources]

        # Build enriched summary with preview and resource keys
        parts = [f"Uploaded {filename} ({len(resources)} chunks, {char_count} chars)."]
        if resource_keys:
            parts.append(f"Resources: {', '.join(resource_keys[:5])}")
        if full_text:
            preview = (full_text[:200] + "…") if len(full_text) > 200 else full_text
            parts.append(f"Preview: {preview}")

        # For image uploads: embed thumbnail as base64 data URI (typically ~3KB),
        # plus store the API path for full-res access
        image_uri = None
        file_id_str = str(file_entity.id)
        is_image = file_entity.mime_type and file_entity.mime_type.startswith("image/")

        if is_image and thumb_data:
            b64 = base64.b64encode(thumb_data).decode()
            image_uri = f"data:image/jpeg;base64,{b64}"

        moment = Moment(
            name=f"upload-{stem}",
            moment_type="content_upload",
            summary="\n".join(parts),
            image_uri=image_uri,
            source_session_id=UUID(session_id) if session_id else None,
            metadata={
                "file_id": file_id_str,
                "file_name": filename,
                "resource_keys": resource_keys,
                "source": "upload",
                "chunk_count": len(resources),
                **({"image_url": f"/content/files/{file_id_str}?thumbnail=true"} if is_image else {}),
            },
            tenant_id=tenant_id, user_id=user_id,
        )
        repo = Repository(Moment, self.db, self.encryption)
        [moment] = await repo.upsert(moment)
        return moment

    async def _ensure_upload_session(
        self,
        filename: str, moment: Moment, resources: list[Resource],
        *, session_id: str | None, tenant_id: str | None, user_id: UUID | None,
    ) -> UUID | None:
        """Create an upload session if none was provided, and link the moment to it."""
        if session_id:
            return UUID(session_id)

        session = Session(
            name=f"upload: {filename}",
            agent_name="content-upload", mode="upload",
            user_id=user_id, tenant_id=tenant_id,
            metadata={
                "resource_keys": [r.name for r in resources],
                "moment_id": str(moment.id),
                "source": filename,
            },
        )
        repo = Repository(Session, self.db, self.encryption)
        [session] = await repo.upsert(session)

        await self.db.execute(
            "UPDATE moments SET source_session_id = $1 WHERE id = $2",
            session.id, moment.id,
        )
        return session.id

    # ── Audio / Image processors ──────────────────────────────────────────

    async def _process_audio(
        self, data: bytes, mime_type: str, chunk_max: int, chunk_overlap: int,
    ) -> tuple[str, list[str]]:
        """Transcribe audio via OpenAI Whisper, then re-chunk as text."""
        if not self.settings.openai_api_key:
            logger.warning("No openai_api_key configured — skipping audio transcription")
            return ("", [])

        full_text = await self._transcribe_audio(data, mime_type)
        # Re-chunk the transcript through the standard document path
        _, chunks = await self._process_document(
            full_text.encode(), "text/plain", chunk_max, chunk_overlap,
        )
        return full_text, chunks

    async def _transcribe_audio(self, data: bytes, mime_type: str) -> str:
        """Split audio on silence and transcribe each segment via Whisper API."""
        import httpx
        from pydub import AudioSegment
        from pydub.silence import split_on_silence
        from pydub.utils import make_chunks

        fmt = mime_type.split("/")[-1]
        if fmt == "mpeg":
            fmt = "mp3"

        audio = AudioSegment.from_file(BytesIO(data), format=fmt)
        segments = split_on_silence(
            audio,
            min_silence_len=self.settings.audio_min_silence_len,
            silence_thresh=self.settings.audio_silence_thresh,
        )
        if len(segments) <= 1:
            segments = make_chunks(audio, self.settings.audio_chunk_duration_ms)

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

        return " ".join(transcriptions)

    async def _process_image(self, data: bytes, mime_type: str) -> tuple[str, list[str]]:
        """No text extraction for images yet. Thumbnail generated separately."""
        # TODO: LLM vision API to describe image
        return ("", [])

    async def _generate_thumbnail(self, data: bytes) -> bytes | None:
        """Generate a JPEG thumbnail (max 400px, 80% quality). Returns None on failure."""
        import asyncio

        def _make_thumb() -> bytes | None:
            try:
                from PIL import Image, ImageOps
                img: Image.Image = Image.open(BytesIO(data))
                img = ImageOps.exif_transpose(img)  # type: ignore[assignment]
                img.thumbnail((400, 400))
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
            except Exception:
                logger.warning("Thumbnail generation failed", exc_info=True)
                return None

        return await asyncio.to_thread(_make_thumb)

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
