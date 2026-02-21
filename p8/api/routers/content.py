"""Content upload endpoint — file → extract → chunk → persist.

Small files (below ``file_processing_threshold_bytes``) are processed inline
during the request.  Larger files are uploaded to S3 and enqueued for a
background worker via QueueService.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, Form
from fastapi.responses import Response

from p8.api.deps import CurrentUser, get_db, get_encryption, get_optional_user
from p8.ontology.types import File as FileEntity
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository
from p8.services.usage import check_quota, get_user_plan

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/", status_code=201)
async def upload_content(
    request: Request,
    file: UploadFile,
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    category: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    s3_key: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
):
    """Upload a file for content extraction, chunking, and persistence.

    Authenticated via JWT or x-user-id/x-tenant-id headers.
    Pass ``session_id`` to link the upload moment to an existing chat session
    so the agent has context about the uploaded content.

    Files smaller than ``file_processing_threshold_bytes`` are processed inline
    and the response includes chunks and resource IDs.  Larger files are
    uploaded to S3 and queued for a background worker — the response includes
    the file entity and a ``task_id``.
    """
    settings = request.app.state.settings
    content_service = request.app.state.content_service
    data = await file.read()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Storage quota check (only when user is identified)
    if user:
        plan_id = await get_user_plan(db, user.user_id, user.tenant_id)
        status = await check_quota(db, user.user_id, "storage_bytes", plan_id)
        if status.used + len(data) > status.limit:
            raise HTTPException(
                429,
                detail={
                    "error": "storage_quota_exceeded",
                    "used": status.used,
                    "limit": status.limit,
                    "file_size": len(data),
                    "message": "Storage limit reached. Upgrade your plan for more storage.",
                },
            )

    threshold = settings.file_processing_threshold_bytes

    if len(data) <= threshold:
        # ── Inline processing ──────────────────────────────────────────
        result = await content_service.ingest(
            data,
            file.filename or "upload",
            mime_type=file.content_type,
            s3_key=s3_key,
            session_id=session_id,
            tenant_id=user.tenant_id if user else None,
            user_id=user.user_id if user else None,
            category=category,
            tags=tag_list,
        )
        return {
            "file": result.file.model_dump(mode="json"),
            "chunk_count": result.chunk_count,
            "total_chars": result.total_chars,
            "resource_ids": [str(r.id) for r in result.resources],
            "session_id": str(result.session_id) if result.session_id else None,
        }

    # ── Queued processing ──────────────────────────────────────────────
    file_service = request.app.state.file_service
    queue_service = request.app.state.queue_service

    filename = file.filename or "upload"
    mime_type = file.content_type
    key = s3_key or filename
    uri = await file_service.write_to_bucket(key, data)

    # Persist a File entity so enqueue_file_task can look it up
    from p8.ontology.types import File as FileEntity
    from p8.services.repository import Repository

    file_entity = FileEntity(
        name=Path(filename).stem,
        uri=uri,
        mime_type=mime_type,
        size_bytes=len(data),
        tenant_id=user.tenant_id if user else None,
        user_id=user.user_id if user else None,
        tags=tag_list,
    )
    encryption = request.app.state.encryption
    file_repo = Repository(FileEntity, db, encryption)
    [file_entity] = await file_repo.upsert(file_entity)

    task_id: UUID = await queue_service.enqueue_file(
        file_entity.id,
        user_id=user.user_id if user else None,
        tenant_id=user.tenant_id if user else None,
    )
    log.info(
        "Queued file %s (%d bytes) as task %s",
        filename, len(data), task_id,
    )

    return {
        "file": file_entity.model_dump(mode="json"),
        "task_id": str(task_id),
        "status": "queued",
    }


@router.get("/files/{file_id}")
async def download_file(
    file_id: UUID,
    request: Request,
    thumbnail: bool = Query(False),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Serve an uploaded file's bytes from S3 (or local storage).

    Pass ``?thumbnail=true`` to get the generated thumbnail instead of the
    original file.  Falls back to the original if no thumbnail exists.
    """
    repo = Repository(FileEntity, db, encryption)
    file_entity = await repo.get(file_id)
    if not file_entity:
        raise HTTPException(status_code=404, detail="File not found")
    if not file_entity.uri:
        raise HTTPException(status_code=404, detail="File has no storage URI")

    file_service = request.app.state.file_service

    # Serve thumbnail if requested and available
    if thumbnail and file_entity.thumbnail_uri:
        data = await file_service.read(file_entity.thumbnail_uri)
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'inline; filename="{file_entity.name}-thumb.jpg"',
                "Cache-Control": "public, max-age=86400",
            },
        )

    data = await file_service.read(file_entity.uri)
    return Response(
        content=data,
        media_type=file_entity.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{file_entity.name}"'},
    )
