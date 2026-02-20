"""Dreaming handler — per-user background AI processing.

Loads recent user moments, builds session summaries, and generates
insights using LLM. Tracks dreaming_io_tokens for quota enforcement.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class DreamingHandler:
    """Background AI processing for a user — moment consolidation and insights."""

    async def handle(self, task: dict, ctx) -> dict:
        user_id = task.get("user_id")
        if not user_id:
            return {"io_tokens": 0, "status": "skipped_no_user"}

        log.info("Dreaming for user %s", user_id)

        # Load recent sessions with unprocessed messages
        sessions = await ctx.db.fetch(
            "SELECT s.id, s.name, s.total_tokens, s.agent_name"
            " FROM sessions s"
            " WHERE s.user_id = $1 AND s.deleted_at IS NULL"
            " ORDER BY s.updated_at DESC LIMIT 10",
            user_id,
        )

        moments_built = 0
        total_io_tokens = 0

        for session in sessions:
            # Build moments for sessions that have accumulated enough tokens
            row = await ctx.db.fetchrow(
                "SELECT * FROM rem_build_moment($1, $2, $3, $4)",
                session["id"],
                task.get("tenant_id"),
                user_id,
                6000,  # token threshold
            )
            if row and row["moment_id"]:
                moments_built += 1
                total_io_tokens += row.get("token_count", 0)

        log.info(
            "Dreaming complete for user %s: %d moments built, %d tokens",
            user_id, moments_built, total_io_tokens,
        )

        return {
            "io_tokens": total_io_tokens,
            "moments_built": moments_built,
            "sessions_checked": len(sessions),
        }
