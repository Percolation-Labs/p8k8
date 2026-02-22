"""File processing handler — download from S3 -> ContentService.ingest() -> track bytes."""

from __future__ import annotations

import logging
from uuid import UUID

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

        # Mark file as processing
        if file_id:
            await ctx.db.execute(
                "UPDATE files SET processing_status = 'processing' WHERE id = $1",
                UUID(file_id) if isinstance(file_id, str) else file_id,
            )

        try:
            # Download file content
            if not uri:
                log.warning("File %s has no URI, skipping download", file_id)
                await self._update_file_status(ctx.db, file_id, "failed")
                return {"bytes_processed": 0, "chunks": 0, "status": "skipped_no_uri"}

            data = await ctx.file_service.read(uri)

            # Ingest via ContentService (extract, chunk, persist)
            result = await ctx.content_service.ingest(
                data,
                name,
                mime_type=payload.get("mime_type"),
                s3_key=None,  # already uploaded
                tenant_id=task.get("tenant_id"),
                user_id=task.get("user_id"),
            )

            # Update original file with parsed content and mark completed
            fid = UUID(file_id) if isinstance(file_id, str) else file_id
            await ctx.db.execute(
                "UPDATE files SET processing_status = 'completed',"
                " parsed_content = $2, updated_at = CURRENT_TIMESTAMP"
                " WHERE id = $1",
                fid,
                result.file.parsed_content,
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

        except Exception:
            await self._update_file_status(ctx.db, file_id, "failed")
            raise

    @staticmethod
    async def _update_file_status(db, file_id, status: str) -> None:
        if file_id:
            fid = UUID(file_id) if isinstance(file_id, str) else file_id
            await db.execute(
                "UPDATE files SET processing_status = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                status, fid,
            )
