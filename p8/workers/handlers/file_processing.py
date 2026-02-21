"""File processing handler — download from S3 -> ContentService.ingest() -> track bytes."""

from __future__ import annotations

import logging

from p8.utils.parsing import extract_payload

log = logging.getLogger(__name__)


class FileProcessingHandler:
    """Process uploaded files: extract text, chunk, persist resources."""

    async def handle(self, task: dict, ctx) -> dict:
        payload = extract_payload(task)
        file_id = payload.get("file_id")
        uri = payload.get("uri")
        name = payload.get("name", "unknown")
        size_bytes = payload.get("size_bytes", 0)

        log.info("Processing file %s (%s, %d bytes)", file_id, name, size_bytes)

        # Download file content
        if uri:
            data = await ctx.file_service.read(uri)
        else:
            # No S3 URI — file content may be stored inline or not yet uploaded
            log.warning("File %s has no URI, skipping download", file_id)
            return {"bytes_processed": 0, "chunks": 0, "status": "skipped_no_uri"}

        # Ingest via ContentService (extract, chunk, persist)
        result = await ctx.content_service.ingest(
            data,
            name,
            mime_type=payload.get("mime_type"),
            s3_key=None,  # already uploaded
            tenant_id=task.get("tenant_id"),
            user_id=task.get("user_id"),
        )

        log.info(
            "File %s processed: %d chunks, %d chars",
            file_id, result.chunk_count, result.total_chars,
        )

        return {
            "bytes_processed": size_bytes,
            "chunks": result.chunk_count,
            "total_chars": result.total_chars,
            "file_id": str(result.file.id),
        }
