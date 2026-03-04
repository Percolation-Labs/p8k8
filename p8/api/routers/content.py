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

    # Fix MIME type: clients often send application/octet-stream; fall back to
    # filename-based detection so Kreuzberg gets a usable type.
    from p8.services.files import FileService
    mime = file.content_type
    if not mime or mime == "application/octet-stream":
        mime = FileService.mime_type_from_path(file.filename or "upload")
    file.headers = file.headers  # keep original headers untouched

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
        from p8.services.content import ContentProcessingError

        try:
            result = await content_service.ingest(
                data,
                file.filename or "upload",
                mime_type=mime,
                s3_key=s3_key,
                session_id=session_id,
                tenant_id=user.tenant_id if user else None,
                user_id=user.user_id if user else None,
                category=category,
                tags=tag_list,
            )
        except ContentProcessingError as e:
            log.warning("Content processing failed: %s (code=%s)", e, e.code)
            raise HTTPException(
                status_code=422,
                detail={"error": e.code, "message": str(e)},
            )
        except Exception as e:
            log.exception("Unexpected error during content ingestion")
            raise HTTPException(
                status_code=500,
                detail={"error": "ingestion_failed", "message": str(e)},
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
    mime_type = mime
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


@router.post("/analyse")
async def analyse_content_endpoint(
    request: Request,
    image: UploadFile | None = None,
    query: str = Form(default="Analyse this content"),
    descriptor: str | None = Form(default=None),
    user: CurrentUser | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    """Analyse content via multimodal LLM — accepts images or content descriptors.

    **Two modes:**

    1. **Image mode** — upload an image file (e.g. rendered PDF page, screenshot).
    2. **Descriptor mode** — pass a JSON content descriptor referencing Percolate
       entities. The server resolves resources/moments and assembles context.

    Descriptor schema::

        {
            "flow": "note" | "pdf_page" | "generic",
            "moment_id": "uuid",          // optional — moment for context
            "resources": [                // optional — files to include
                "uuid",                   // simple: whole file
                {"id": "uuid", "pages": [12, 13]}  // with page selection (0-indexed)
            ],
            "context": { ... }            // optional — extra metadata as text
        }

    Both modes can be combined (image + descriptor for enriched context).
    """
    import json as _json

    from p8.services.vision import ContentItem, analyse_content, analyse_image

    items: list[ContentItem] = []
    result_meta: dict = {}

    # ── Image mode ────────────────────────────────────────────────────
    if image:
        data = await image.read()
        if not data:
            raise HTTPException(status_code=400, detail="Empty image")
        media_type = image.content_type or "image/png"
        if media_type == "application/octet-stream":
            media_type = "image/png"
        items.append(ContentItem(image_data=data, media_type=media_type, label="uploaded image"))

    # ── Descriptor mode ───────────────────────────────────────────────
    desc = None
    if descriptor:
        try:
            desc = _json.loads(descriptor)
        except _json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid descriptor JSON")

        flow = desc.get("flow", "generic")
        moment_id = desc.get("moment_id")
        resources = desc.get("resources", [])
        context = desc.get("context")

        result_meta["flow"] = flow
        result_meta["moment_id"] = moment_id

        # Resolve moment metadata as text context
        if moment_id:
            await _add_moment_context(items, moment_id, db, encryption)

        # Resolve resources — each can be a plain UUID string or
        # {"id": "uuid", "pages": [0, 1, 2]} for PDF page selection
        for res in resources:
            if isinstance(res, str):
                items.append(ContentItem(uri=res, label=f"resource {res}"))
            elif isinstance(res, dict):
                rid = res.get("id", "")
                res_pages = res.get("pages")
                label = f"resource {rid}"
                if res_pages:
                    label += f" pages {res_pages}"
                items.append(ContentItem(uri=rid, pages=res_pages, label=label))

        # Include arbitrary context dict as text
        if context:
            items.append(ContentItem(
                text=f"Additional context:\n{_json.dumps(context, indent=2)}",
                label="context",
            ))

    if not items:
        raise HTTPException(status_code=400, detail="No content provided — upload an image or pass a descriptor")

    result = await analyse_content(items, query)
    result.update(result_meta)
    return result


async def _add_moment_context(
    items: list,
    moment_id: str,
    db: Database,
    encryption: EncryptionService,
) -> None:
    """Resolve a moment and add its metadata as text context to the item list."""
    import json as _json
    from uuid import UUID

    from p8.ontology.types import Moment
    from p8.services.repository import Repository
    from p8.services.vision import ContentItem

    try:
        mid = UUID(moment_id)
    except ValueError:
        return

    repo = Repository(Moment, db, encryption)
    moment = await repo.get(mid)
    if not moment:
        return

    # Include moment summary
    if moment.summary:
        items.append(ContentItem(text=f"Document summary: {moment.summary}", label="moment summary"))

    # Include annotation metadata (stickers, drawings, etc.)
    if moment.metadata:
        meta = dict(moment.metadata)
        meta.pop("file_id", None)  # don't leak internal IDs to the LLM
        if meta:
            items.append(ContentItem(
                text=f"Annotation metadata:\n{_json.dumps(meta, indent=2, default=str)}",
                label="annotations",
            ))
