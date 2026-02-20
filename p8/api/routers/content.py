"""Content upload endpoint — file → extract → chunk → persist."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, UploadFile, Form

from p8.api.deps import CurrentUser, get_optional_user

router = APIRouter()


@router.post("/", status_code=201)
async def upload_content(
    request: Request,
    file: UploadFile,
    user: CurrentUser | None = Depends(get_optional_user),
    category: str | None = Form(default=None),
    tags: str | None = Form(default=None),
    s3_key: str | None = Form(default=None),
):
    """Upload a file for content extraction, chunking, and persistence.

    Authenticated via JWT or x-user-id/x-tenant-id headers.
    Returns the created File entity, chunk count, and Resource IDs.
    """
    content_service = request.app.state.content_service
    data = await file.read()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    result = await content_service.ingest(
        data,
        file.filename or "upload",
        mime_type=file.content_type,
        s3_key=s3_key,
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
