"""Content upload endpoint — file → extract → chunk → persist."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, Form

from p8.api.deps import CurrentUser, get_db, get_optional_user
from p8.services.database import Database
from p8.services.usage import check_quota, get_user_plan

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
):
    """Upload a file for content extraction, chunking, and persistence.

    Authenticated via JWT or x-user-id/x-tenant-id headers.
    Returns the created File entity, chunk count, and Resource IDs.
    """
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
