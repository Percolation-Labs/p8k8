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
from p8.ontology.types import File, Ontology, Resource
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.files import FileService
from p8.services.memory import MemoryService
from p8.services.repository import Repository
from p8.settings import Settings

logger = logging.getLogger(__name__)


class ContentProcessingError(Exception):
    """Raised when content extraction/processing fails with a classifiable cause."""

    def __init__(self, message: str, *, code: str = "processing_failed"):
        super().__init__(message)
        self.code = code


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


_LINK_SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:")

# Whisper API limit is 25 MB; we stay at 24 MB for safety.
_WHISPER_MAX_BYTES = 24 * 1024 * 1024


def chunk_audio_for_whisper(
    audio: "AudioSegment",  # type: ignore[name-defined]  # noqa: F821
    *,
    silence_thresh: int = -40,
    min_silence_len: int = 700,
    target_chunk_ms: int = 60_000,
) -> list[BytesIO]:
    """Prepare audio buffers for the Whisper API.

    Strategy:
      1. Export the full audio as WAV.  If it fits under the 24 MB Whisper
         limit, return it as a single buffer — no splitting needed.
      2. Otherwise, split at silence boundaries and merge adjacent segments
         until each chunk is close to ``target_chunk_ms`` (~60 s).  This
         keeps the number of API calls low while landing on natural pauses.

    Silence detection uses a **dynamic threshold**: whichever is lower of
    the configured ``silence_thresh`` or ``audio.dBFS - 16``.  A fixed
    threshold (e.g. -40 dBFS) can misclassify quiet speech as silence in
    phone recordings where the average level is below -40.

    Args:
        audio: A pydub AudioSegment.
        silence_thresh: Absolute dBFS floor for silence detection.
        min_silence_len: Minimum ms of silence to trigger a split point.
        target_chunk_ms: Target duration per chunk when splitting is needed.

    Returns:
        List of BytesIO buffers, each containing WAV data ready for Whisper.
    """
    from pydub.silence import split_on_silence
    from pydub.utils import make_chunks

    # Try sending the whole file in one shot.
    full_buf = BytesIO()
    audio.export(full_buf, format="wav")
    if full_buf.tell() <= _WHISPER_MAX_BYTES:
        full_buf.seek(0)
        return [full_buf]

    # File too large — split at silence boundaries near ~60 s marks.
    # Dynamic threshold: quiet recordings need a lower threshold so that
    # actual speech isn't treated as silence.
    effective_thresh = min(silence_thresh, audio.dBFS - 16)

    raw_segments = split_on_silence(
        audio,
        min_silence_len=min_silence_len,
        silence_thresh=effective_thresh,
        keep_silence=300,  # preserve 300 ms padding to avoid clipping words
    )
    if len(raw_segments) <= 1:
        # No silence found (e.g. music, constant noise) — fall back to
        # fixed-duration cuts.
        raw_segments = make_chunks(audio, target_chunk_ms)

    # Merge small segments so each chunk is close to the target duration.
    # This avoids sending dozens of tiny requests to Whisper.
    from pydub import AudioSegment as _AS

    merged: list = []
    current = _AS.empty()
    for seg in raw_segments:
        if len(current) + len(seg) > target_chunk_ms and len(current) >= 500:
            merged.append(current)
            current = seg
        else:
            current += seg
    if len(current) >= 500:
        merged.append(current)

    # Export each merged segment as WAV.
    buffers: list[BytesIO] = []
    for seg in merged:
        buf = BytesIO()
        seg.export(buf, format="wav")
        buf.seek(0)
        buffers.append(buf)

    return buffers


def _links_to_edges(links: list[tuple[int, str, str]]) -> list[dict]:
    """Convert extracted markdown links to graph_edges dicts.

    Each ``[text](target)`` becomes ``{"target": stem, "relation": "links_to", "weight": 1.0}``.
    URLs, anchors, and data URIs are skipped.  Duplicates are deduplicated by target.
    """
    seen: set[str] = set()
    edges: list[dict] = []
    for _line, _text, target in links:
        if any(target.startswith(p) for p in _LINK_SKIP_PREFIXES):
            continue
        stem = Path(target).stem
        if stem in seen:
            continue
        seen.add(stem)
        edges.append({"target": stem, "relation": "links_to", "weight": 1.0})
    return edges


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
        create_moment: bool = True,
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

        result_session_id: UUID | None = None

        if create_moment:
            # Build moment + companion session via unified create_moment_session
            resource_keys = [r.name for r in resource_entities]
            file_id_str = str(file_entity.id)
            is_image = mime_type and mime_type.startswith("image/")

            # Build summary — user-facing content only, no metadata
            char_count = len(full_text) if full_text else 0
            if full_text:
                summary = (full_text[:300] + "…") if len(full_text) > 300 else full_text
            else:
                summary = f"Uploaded {filename}"

            # Build image_uri for thumbnails
            image_uri = None
            if is_image and thumb_data:
                import base64
                b64 = base64.b64encode(thumb_data).decode()
                image_uri = f"data:image/jpeg;base64,{b64}"

            moment_metadata = {
                "file_id": file_id_str,
                "file_name": filename,
                "resource_keys": resource_keys,
                "source": "upload",
                "chunk_count": len(resource_entities),
                "char_count": char_count,
                **({"image_url": f"/content/files/{file_id_str}?thumbnail=true"} if is_image else {}),
            }

            memory = MemoryService(self.db, self.encryption)
            moment, session = await memory.create_moment_session(
                name=f"upload-{stem}",
                moment_type="content_upload",
                summary=summary,
                metadata=moment_metadata,
                image_uri=image_uri,
                session_id=UUID(session_id) if session_id else None,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            result_session_id = session.id

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
result = extract_file_sync(Path(sys.argv[1]), mime_type="{mime_type}", config=config)

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
            "text/csv": ".csv",
            "text/tab-separated-values": ".tsv",
        }
        import mimetypes
        return _map.get(mime_type) or mimetypes.guess_extension(mime_type) or ".bin"

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

    # ── Audio / Image processors ──────────────────────────────────────────

    async def _process_audio(
        self, data: bytes, mime_type: str, chunk_max: int, chunk_overlap: int,
    ) -> tuple[str, list[str]]:
        """Transcribe audio via OpenAI Whisper, then re-chunk as text."""
        if not self.settings.openai_api_key:
            raise ContentProcessingError("Audio transcription requires an OpenAI API key", code="no_api_key")

        full_text = await self._transcribe_audio(data, mime_type)
        if not full_text.strip():
            return ("", [])
        # Re-chunk the transcript through the standard document path
        _, chunks = await self._process_document(
            full_text.encode(), "text/plain", chunk_max, chunk_overlap,
        )
        return full_text, chunks

    async def _transcribe_audio(self, data: bytes, mime_type: str) -> str:
        """Transcribe audio via Whisper API, chunking only when necessary."""
        import httpx
        from pydub import AudioSegment

        fmt = mime_type.split("/")[-1]
        fmt_map = {"mpeg": "mp3", "x-m4a": "m4a", "mp4": "m4a", "x-wav": "wav", "ogg": "ogg"}
        fmt = fmt_map.get(fmt, fmt)

        try:
            audio = AudioSegment.from_file(BytesIO(data), format=fmt)
        except Exception as e:
            raise ContentProcessingError(
                f"Could not decode audio ({mime_type}): {e}", code="audio_decode_failed",
            ) from e

        buffers = chunk_audio_for_whisper(
            audio,
            silence_thresh=self.settings.audio_silence_thresh,
            min_silence_len=self.settings.audio_min_silence_len,
            target_chunk_ms=self.settings.audio_chunk_duration_ms,
        )
        if not buffers:
            logger.info("No audio segments to transcribe (%d ms total)", len(audio))
            return ""

        transcriptions: list[str] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i, buf in enumerate(buffers):
                try:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                        data={"model": "whisper-1", "response_format": "text"},
                        files={"file": ("chunk.wav", buf, "audio/wav")},
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    logger.warning("Whisper API error on segment %d/%d: %s %s",
                                   i + 1, len(buffers), e.response.status_code, e.response.text[:200])
                    raise ContentProcessingError(
                        f"Transcription failed (segment {i + 1}/{len(buffers)}): {e.response.status_code}",
                        code="transcription_failed",
                    ) from e
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
        Markdown links ``[text](target)`` are parsed into ``graph_edges``
        so the knowledge graph is traversable via ``rem_traverse()``.
        """
        from p8.utils.links import extract_links

        cls = model_class or Ontology
        entities = []
        for fp in paths:
            text = await self.file_service.read_text(fp)
            p = Path(fp)
            # README files are named for their parent folder
            stem = p.parent.name if p.stem.lower() == "readme" else p.stem

            # Parse markdown links → graph_edges
            graph_edges = _links_to_edges(extract_links(text))

            entity = cls.model_validate({
                "name": stem,
                "content": text,
                "graph_edges": graph_edges,
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
        """Validate and upsert structured data (from JSON/YAML) into a table.

        When *user_id* is provided it **overrides** every row's user_id and
        drops any explicit ``id`` so deterministic IDs recompute for the target
        user.  This lets you write seed data once and apply it to any user.
        Without *user_id*, rows keep whatever user_id is in the data.
        """
        entities = []
        for item in items:
            if tenant_id:
                item.setdefault("tenant_id", tenant_id)
            if user_id:
                item["user_id"] = str(user_id)  # force override
                item.pop("id", None)  # drop so deterministic ID recomputes
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


async def generate_mosaic_thumbnail(
    image_uris: list[str], *, size: int = 400, quality: int = 82
) -> str | None:
    """Download up to 6 images and tile them into a blended mosaic JPEG.

    Layout: 3x2 grid (or fewer tiles if less images available).
    Each tile is center-cropped to fill its cell — no whitespace.
    A soft gradient overlay blends the tiles together for a unified look.

    Returns a ``data:image/jpeg;base64,...`` data URI, or *None* on failure.
    """
    import asyncio
    import base64

    import httpx

    uris = [u for u in image_uris if u and u.startswith("http")][:6]
    if not uris:
        return None

    async def _download(url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=5) as c:
                resp = await c.get(url)
                if resp.status_code == 200:
                    return resp.content
        except Exception:
            pass
        return None

    results = await asyncio.gather(*[_download(u) for u in uris])
    blobs = [b for b in results if b]
    if not blobs:
        return None

    def _compose(blobs: list[bytes]) -> bytes | None:
        try:
            from PIL import Image, ImageFilter, ImageOps

            imgs: list[Image.Image] = []
            for b in blobs:
                img = Image.open(BytesIO(b))
                img = ImageOps.exif_transpose(img)  # type: ignore[assignment]
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")  # type: ignore[assignment]
                imgs.append(img)

            n = len(imgs)

            # Pick grid layout based on image count
            if n == 1:
                cols, rows = 1, 1
            elif n == 2:
                cols, rows = 2, 1
            elif n <= 4:
                cols, rows = 2, 2
            else:
                cols, rows = 3, 2

            canvas_w = size
            canvas_h = size * rows // cols  # maintain roughly square for 2x2, wider for 3x2
            cell_w = canvas_w // cols
            cell_h = canvas_h // rows

            canvas = Image.new("RGB", (canvas_w, canvas_h), (30, 30, 35))

            for i, im in enumerate(imgs[: cols * rows]):
                c = i % cols
                r = i // cols
                # Center-crop to fill cell
                tile = ImageOps.fit(im, (cell_w, cell_h))  # type: ignore[arg-type]
                canvas.paste(tile, (c * cell_w, r * cell_h))

            # Soft blur to blend tile edges
            canvas = canvas.filter(ImageFilter.GaussianBlur(radius=0.8))  # type: ignore[assignment]

            buf = BytesIO()
            canvas.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
        except Exception:
            logger.warning("Mosaic thumbnail generation failed", exc_info=True)
            return None

    thumb_bytes = await asyncio.to_thread(_compose, blobs)
    if not thumb_bytes:
        return None

    b64 = base64.b64encode(thumb_bytes).decode()
    return f"data:image/jpeg;base64,{b64}"
