"""Drive sync handler — syncs files from a user's Google Drive folder.

Reads the user's StorageGrant to find the selected folder, then delegates
to GoogleDriveService.sync_folder() which downloads and ingests files.
After syncing, creates a summary moment (type=drive_sync) that groups
the individual file uploads. This moment is NOT shown in the main feed
by default — it serves as a drilldown container for file upload moments.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from p8.services.memory import MemoryService
from p8.services.providers.gdrive import GoogleDriveService

log = logging.getLogger(__name__)


class DriveSyncHandler:
    """Background handler for drive_sync tasks."""

    async def handle(self, task: dict, ctx) -> dict:
        user_id = task.get("user_id")
        if not user_id:
            return {"status": "skipped_no_user"}

        if isinstance(user_id, str):
            user_id = UUID(user_id)

        tenant_id = task.get("tenant_id")
        log.info("Drive sync for user %s", user_id)

        # Look up the active grant with a selected folder
        row = await ctx.db.fetchrow(
            "SELECT id, provider_folder_id, folder_name FROM storage_grants"
            " WHERE user_id_ref = $1 AND provider = 'google-drive' AND status = 'active'"
            " AND provider_folder_id IS NOT NULL"
            " LIMIT 1",
            user_id,
        )
        if not row:
            return {"status": "skipped_no_grant"}

        folder_id = row["provider_folder_id"]
        folder_name = row["folder_name"]
        log.info("Syncing folder '%s' (%s) for user %s", folder_name, folder_id, user_id)

        gdrive = GoogleDriveService(
            db=ctx.db,
            encryption=ctx.encryption,
            settings=ctx.settings,
            content_service=ctx.content_service,
        )

        result = await gdrive.sync_folder(
            user_id,
            tenant_id or "",
            folder_id=folder_id,
        )

        log.info(
            "Drive sync complete for user %s: synced=%d updated=%d skipped=%d errors=%d",
            user_id, result.synced, result.updated, result.skipped, result.errors,
        )

        # Create a summary moment when files were synced or updated
        if result.synced > 0 or result.updated > 0:
            await self._create_sync_moment(
                ctx, user_id, tenant_id, folder_name or "Drive", result,
            )

        return {
            "status": "ok",
            "synced": result.synced,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors": result.errors,
            "files": result.files,
        }

    async def _create_sync_moment(
        self, ctx, user_id: UUID, tenant_id: str | None,
        folder_name: str, result,
    ) -> None:
        """Create a drive_sync moment summarising the batch of synced files."""
        try:
            file_list = "\n".join(f"- {f}" for f in result.files)
            count = result.synced + result.updated
            plural = "file" if count == 1 else "files"
            parts = []
            if result.synced:
                parts.append(f"{result.synced} new")
            if result.updated:
                parts.append(f"{result.updated} updated")
            action = ", ".join(parts) if parts else f"{count}"
            summary = (
                f"Synced {action} {plural} from Google Drive folder **{folder_name}**.\n\n"
                f"{file_list}"
            )
            if result.errors:
                summary += f"\n\n{result.errors} file(s) had errors during sync."

            memory = MemoryService(ctx.db, ctx.encryption)
            await memory.create_moment_session(
                name=f"drive-sync-{folder_name}",
                moment_type="drive_sync",
                summary=summary,
                user_id=user_id,
                tenant_id=tenant_id,
                topic_tags=["google-drive", "sync", folder_name],
                metadata={
                    "folder_name": folder_name,
                    "synced_count": result.synced,
                    "skipped_count": result.skipped,
                    "error_count": result.errors,
                    "files": result.files,
                    "file_ids": result.file_ids,
                    "file_map": dict(zip(result.files, result.file_ids)),
                    "provider": "google-drive",
                },
            )
            log.info("Created drive_sync moment for user %s (%d files)", user_id, count)
        except Exception:
            log.exception("Failed to create drive_sync moment for user %s", user_id)
