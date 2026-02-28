"""Google Drive provider — list, download, and sync files.

Uses the stored refresh token from StorageGrant to obtain short-lived
access tokens. Tracks sync state via StorageGrant.sync_cursor (Drive
changes API startPageToken) and marks synced files with provider metadata
so we can filter by origin and avoid re-syncing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import httpx

from p8.ontology.types import File, StorageGrant
from p8.services.content import ContentService
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository
from p8.settings import Settings

logger = logging.getLogger(__name__)

GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# MIME types we can extract text from
SYNCABLE_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/json",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
}

# Google Docs export MIME mappings
EXPORT_MIME = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


@dataclass
class DriveFile:
    """Lightweight representation of a Google Drive file."""

    id: str
    name: str
    mime_type: str
    size: int | None = None
    modified_time: str | None = None
    parents: list[str] = field(default_factory=list)


@dataclass
class SyncResult:
    """Summary of a sync operation."""

    synced: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    files: list[str] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)


class GoogleDriveService:
    """Google Drive provider for file listing and sync."""

    def __init__(
        self,
        db: Database,
        encryption: EncryptionService,
        settings: Settings,
        content_service: ContentService,
    ):
        self.db = db
        self.encryption = encryption
        self.settings = settings
        self.content_service = content_service
        self._grants_repo = Repository(StorageGrant, db, encryption)

    async def _get_access_token(self, grant: StorageGrant) -> str:
        """Exchange the stored refresh token for a fresh access token."""
        refresh_token = (grant.metadata or {}).get("refresh_token")
        if not refresh_token:
            raise ValueError("StorageGrant has no refresh_token in metadata")

        async with httpx.AsyncClient() as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            resp.raise_for_status()
            return resp.json()["access_token"]  # type: ignore[no-any-return]

    async def _get_grant(self, user_id: UUID) -> StorageGrant:
        """Get the active Google Drive grant for a user."""
        row = await self.db.fetchrow(
            "SELECT * FROM storage_grants"
            " WHERE user_id_ref = $1 AND provider = 'google-drive' AND status = 'active'"
            " LIMIT 1",
            user_id,
        )
        if not row:
            raise ValueError("No active Google Drive grant for this user")
        return StorageGrant(**dict(row))

    async def list_files(
        self,
        user_id: UUID,
        *,
        folder_id: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[DriveFile], str | None]:
        """List files in a folder (or root). Returns (files, next_page_token)."""
        grant = await self._get_grant(user_id)
        access_token = await self._get_access_token(grant)

        parent = folder_id or "root"
        q = f"'{parent}' in parents and trashed = false"

        params: dict[str, str] = {
            "q": q,
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, parents)",
            "pageSize": str(page_size),
            "orderBy": "modifiedTime desc",
        }
        if page_token:
            params["pageToken"] = page_token

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        files = [
            DriveFile(
                id=f["id"],
                name=f["name"],
                mime_type=f["mimeType"],
                size=int(f["size"]) if f.get("size") else None,
                modified_time=f.get("modifiedTime"),
                parents=f.get("parents", []),
            )
            for f in data.get("files", [])
        ]
        return files, data.get("nextPageToken")

    async def list_folders(
        self,
        user_id: UUID,
        *,
        parent_id: str | None = None,
    ) -> list[DriveFile]:
        """List folders under a parent (or root)."""
        grant = await self._get_grant(user_id)
        access_token = await self._get_access_token(grant)

        parent = parent_id or "root"
        q = f"'{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params={
                    "q": q,
                    "fields": "files(id, name, mimeType, modifiedTime)",
                    "pageSize": "100",
                    "orderBy": "name",
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            DriveFile(id=f["id"], name=f["name"], mime_type=f["mimeType"],
                      modified_time=f.get("modifiedTime"))
            for f in data.get("files", [])
        ]

    async def download_file(
        self,
        user_id: UUID,
        file_id: str,
        *,
        mime_type: str | None = None,
    ) -> tuple[bytes, str, str]:
        """Download a file's content. Returns (bytes, filename, mime_type).

        For Google Docs/Sheets/Slides, exports to PDF/CSV.
        """
        grant = await self._get_grant(user_id)
        access_token = await self._get_access_token(grant)
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient() as client:
            # Get file metadata
            meta_resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files/{file_id}",
                params={"fields": "id, name, mimeType, size"},
                headers=headers,
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()

            name = meta["name"]
            file_mime = mime_type or meta["mimeType"]

            # Google Workspace files need export
            if file_mime in EXPORT_MIME:
                export_mime, ext = EXPORT_MIME[file_mime]
                resp = await client.get(
                    f"{GOOGLE_DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": export_mime},
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.content, f"{name}{ext}", export_mime

            # Regular files — direct download
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files/{file_id}",
                params={"alt": "media"},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.content, name, file_mime

    async def _already_synced(self, user_id: UUID, provider_file_id: str) -> bool:
        """Check if a Drive file has already been synced for this user."""
        row = await self.db.fetchrow(
            "SELECT id FROM files"
            " WHERE user_id = $1 AND metadata->>'provider_file_id' = $2"
            " AND deleted_at IS NULL",
            user_id,
            provider_file_id,
        )
        return row is not None

    async def _synced_file_info(
        self, user_id: UUID, provider_file_id: str,
    ) -> dict | None:
        """Return existing synced file info, or None if not yet synced."""
        row = await self.db.fetchrow(
            "SELECT id, metadata->>'provider_modified_time' AS provider_modified_time"
            " FROM files"
            " WHERE user_id = $1 AND metadata->>'provider_file_id' = $2"
            " AND deleted_at IS NULL",
            user_id,
            provider_file_id,
        )
        return dict(row) if row else None

    async def sync_folder(
        self,
        user_id: UUID,
        tenant_id: str,
        *,
        folder_id: str | None = None,
        force: bool = False,
    ) -> SyncResult:
        """Sync files from a Drive folder into p8.

        Skips files that have already been synced (matched by provider_file_id
        in file metadata) unless force=True.
        """
        result = SyncResult()
        page_token: str | None = None

        while True:
            files, page_token = await self.list_files(
                user_id, folder_id=folder_id, page_token=page_token,
            )

            for df in files:
                # Skip folders
                if df.mime_type == "application/vnd.google-apps.folder":
                    continue

                # Skip non-syncable types
                if df.mime_type not in SYNCABLE_MIME_TYPES:
                    result.skipped += 1
                    continue

                # Check if already synced and whether it's been modified
                existing = await self._synced_file_info(user_id, df.id)
                if existing and not force:
                    # Compare modification times to detect updates
                    stored_mtime = existing.get("provider_modified_time") or ""
                    drive_mtime = df.modified_time or ""
                    if stored_mtime >= drive_mtime:
                        result.skipped += 1
                        continue
                    # File was modified since last sync — re-sync it
                    logger.info(
                        "File modified since last sync: %s (stored=%s, drive=%s)",
                        df.name, stored_mtime, drive_mtime,
                    )
                    is_update = True
                    existing_file_id = existing["id"]
                else:
                    is_update = bool(existing)  # force re-sync of existing
                    existing_file_id = existing["id"] if existing else None

                try:
                    data, filename, mime = await self.download_file(user_id, df.id)

                    if is_update and existing_file_id:
                        # Update existing file: re-ingest content and update metadata
                        ingest_result = await self.content_service.ingest(
                            data,
                            filename,
                            mime_type=mime,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            tags=["google-drive-sync"],
                            create_moment=False,
                        )
                        # Delete the old file entity
                        await self.db.execute(
                            "UPDATE files SET deleted_at = NOW() WHERE id = $1",
                            existing_file_id,
                        )
                    else:
                        ingest_result = await self.content_service.ingest(
                            data,
                            filename,
                            mime_type=mime,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            tags=["google-drive-sync"],
                            create_moment=False,
                        )

                    # Stamp provider origin on the File entity.
                    # Pass dict directly — the custom JSONB codec handles
                    # serialization (json.dumps is the registered encoder).
                    provider_meta = {
                        "provider": "google-drive",
                        "provider_file_id": df.id,
                        "provider_file_name": df.name,
                        "provider_modified_time": df.modified_time or "",
                        "synced_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await self.db.execute(
                        "UPDATE files SET metadata = $1::jsonb WHERE id = $2",
                        provider_meta,
                        ingest_result.file.id,
                    )

                    if is_update:
                        result.updated += 1
                        result.files.append(f"{df.name} (updated)")
                        logger.info("Updated %s (id=%s)", df.name, df.id)
                    else:
                        result.synced += 1
                        result.files.append(df.name)
                        logger.info("Synced %s (id=%s)", df.name, df.id)
                    result.file_ids.append(str(ingest_result.file.id))

                except Exception:
                    result.errors += 1
                    logger.exception("Failed to sync %s (id=%s)", df.name, df.id)

            if not page_token:
                break

        # Update grant sync timestamp
        grant = await self._get_grant(user_id)
        await self.db.execute(
            "UPDATE storage_grants SET last_sync_at = NOW() WHERE id = $1",
            grant.id,
        )

        return result

    async def get_synced_files(
        self,
        user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List files that were synced from Google Drive for this user."""
        rows = await self.db.fetch(
            "SELECT id, name, mime_type, size_bytes, metadata, created_at"
            " FROM files"
            " WHERE user_id = $1 AND metadata->>'provider' = 'google-drive'"
            " AND deleted_at IS NULL"
            " ORDER BY created_at DESC"
            " LIMIT $2 OFFSET $3",
            user_id,
            limit,
            offset,
        )
        return [dict(r) for r in rows]
