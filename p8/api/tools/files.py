"""MCP tool for reading uploaded files from Percolate.

Exposes uploaded file content (CSV, text) via a ``files://{file_id}``
resource URI.  Files are stored in S3 (or locally) and tracked via the
``files`` table.  This tool resolves the entity, fetches content from
storage, and returns structured CSV rows or plain text — the same shape
as platoon's ``read_file`` so agents can use either interchangeably.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from p8.api.tools import get_db, get_encryption, get_user_id
from p8.ontology.types import File as FileEntity, Moment
from p8.services.files import FileService
from p8.services.repository import Repository
from p8.settings import get_settings

log = logging.getLogger(__name__)


def _is_uuid(value: str) -> bool:
    """Check if a string looks like a UUID (file ID)."""
    try:
        UUID(value.strip())
        return True
    except ValueError:
        return False


async def resolve_data_path(data_path: str) -> str:
    """Resolve a data_path to a local file path.

    If ``data_path`` is a UUID, downloads the file from S3 to a temp file
    and returns that path.  Otherwise returns the path unchanged.  This lets
    platoon tools accept either local paths or uploaded file IDs seamlessly.
    """
    if not _is_uuid(data_path):
        return data_path

    file_id = data_path.strip()
    db = get_db()
    encryption = get_encryption()
    repo = Repository(FileEntity, db, encryption)
    entity = await repo.get(UUID(file_id))
    if not entity:
        # Agent may have passed a moment ID instead of the file_id from metadata.
        # Check if it's a content_upload moment and follow through.
        moment_repo = Repository(Moment, db, encryption)
        moment = await moment_repo.get(UUID(file_id))
        if moment and moment.metadata and moment.metadata.get("file_id"):
            real_file_id = moment.metadata["file_id"]
            log.info("Resolved moment %s → file %s", file_id, real_file_id)
            entity = await repo.get(UUID(real_file_id))
    if not entity:
        raise FileNotFoundError(f"Uploaded file not found: {file_id}")
    if not entity.uri:
        raise FileNotFoundError(f"File has no storage URI: {file_id}")

    fs = FileService(get_settings())
    data = await fs.read(entity.uri)

    suffix = Path(entity.name or "").suffix or ".csv"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    return tmp.name


async def get_file(file_id: str, head: int = 0) -> dict[str, Any]:
    """Fetch an uploaded file's content from Percolate.

    Returns CSV rows (like platoon_read_file) or plain text, depending on
    the file type.  Files are uploaded via the Percolate app, drive sync,
    or the POST /content/ API endpoint.

    Tip: Use the Percolate mobile app or drive sync to upload spreadsheets
    and data files for analysis.  Once uploaded, reference them here by ID.

    Args:
        file_id: The UUID of the uploaded file (from POST /content/ response).
        head: If > 0, return only the first N rows/lines.
    """
    db = get_db()
    encryption = get_encryption()

    # Look up the file entity
    try:
        fid = UUID(file_id)
    except ValueError:
        return {"status": "error", "error": f"Invalid file_id: {file_id}"}

    repo = Repository(FileEntity, db, encryption)
    entity = await repo.get(fid)
    if not entity:
        # Agent may have passed a moment ID — resolve via metadata.file_id
        moment_repo = Repository(Moment, db, encryption)
        moment = await moment_repo.get(fid)
        if moment and moment.metadata and moment.metadata.get("file_id"):
            real_file_id = moment.metadata["file_id"]
            log.info("get_file: resolved moment %s → file %s", file_id, real_file_id)
            entity = await repo.get(UUID(real_file_id))
    if not entity:
        return {"status": "error", "error": f"File not found: {file_id}"}

    # For CSV/TSV files, prefer raw bytes from S3 (Kreuzberg strips delimiters
    # during extraction, so parsed_content loses CSV structure).
    suffix = Path(entity.name or "").suffix.lower()
    mime = (entity.mime_type or "").lower()
    is_structured = suffix in (".csv", ".tsv") or "csv" in mime or "tab-separated" in mime

    if is_structured and entity.uri:
        try:
            fs = FileService(get_settings())
            data = await fs.read(entity.uri)
            text = data.decode("utf-8", errors="replace")
            return _format_text(entity, text, head)
        except Exception:
            pass  # fall through to parsed_content

    # For other files, use parsed content (full extracted text)
    if entity.parsed_content:
        return _format_text(entity, entity.parsed_content, head)

    # Last resort: fetch raw bytes from storage
    if not entity.uri:
        return {"status": "error", "error": "File has no content or storage URI"}

    try:
        fs = FileService(get_settings())
        data = await fs.read(entity.uri)
    except Exception as e:
        return {"status": "error", "error": f"Failed to read file: {e}"}

    text = data.decode("utf-8", errors="replace")
    return _format_text(entity, text, head)


def _format_text(entity: FileEntity, text: str, head: int) -> dict[str, Any]:
    """Format file content as CSV rows or plain text."""
    suffix = Path(entity.name or "").suffix.lower()
    mime = (entity.mime_type or "").lower()

    is_csv = suffix == ".csv" or "csv" in mime

    if is_csv:
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if head > 0:
                rows = rows[:head]
            return {
                "status": "ok",
                "format": "csv",
                "file_id": str(entity.id),
                "name": entity.name,
                "columns": list(rows[0].keys()) if rows else [],
                "row_count": len(rows),
                "rows": rows,
            }
        except Exception:
            pass  # fall through to text

    lines = text.splitlines()
    if head > 0:
        lines = lines[:head]
        text = "\n".join(lines)

    return {
        "status": "ok",
        "format": "text",
        "file_id": str(entity.id),
        "name": entity.name,
        "line_count": len(lines),
        "content": text,
    }


async def get_file_resource(file_id: str) -> str:
    """MCP resource handler for files://{file_id}."""
    result = await get_file(file_id)
    return json.dumps(result, default=str)
