"""update_user_metadata tool — structured partial updates to user metadata."""

from __future__ import annotations

import logging
from typing import Any

from p8.api.tools import get_db, get_encryption, get_user_id

logger = logging.getLogger(__name__)


async def update_user_metadata(
    metadata: dict[str, Any],
    remove_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Update structured metadata on the current user's profile.

    Performs a shallow JSON merge — provided keys overwrite existing ones,
    absent keys are preserved. Use ``remove_keys`` to delete top-level keys.

    The metadata schema follows UserMetadata:
      - relations:   list of {"name", "role", "notes"} — family, pets, colleagues
      - interests:   list of topic strings
      - feeds:       list of {"url", "name", "type", "notes"} — RSS, websites
      - preferences: dict of user preferences (timezone, language, etc.)
      - facts:       dict of observed facts (birthday, company, etc.)

    Partial updates are recommended — only send the keys that changed.

    Examples:
      Add a pet:
        metadata={"relations": [{"name": "Luna", "role": "pet", "notes": "golden retriever"}]}

      Update timezone:
        metadata={"preferences": {"timezone": "US/Pacific"}}

      Remove feeds section:
        metadata={}, remove_keys=["feeds"]

    Args:
        metadata: Dict of fields to merge into user metadata. Supports all
            UserMetadata fields: relations, interests, feeds, preferences, facts.
        remove_keys: Top-level metadata keys to delete.

    Returns:
        Dict with status and the full updated metadata.
    """
    from p8.ontology.types import User, UserMetadata
    from p8.services.repository import Repository

    user_id = get_user_id()
    if not user_id:
        return {"status": "error", "error": "user_id is required"}

    # Validate against UserMetadata schema
    try:
        UserMetadata.model_validate(metadata)
    except Exception as exc:
        return {"status": "error", "error": f"Invalid metadata shape: {exc}"}

    db = get_db()
    encryption = get_encryption()
    repo = Repository(User, db, encryption)

    try:
        result = await repo.merge_metadata(
            user_id, metadata, remove_keys=remove_keys,
        )
    except Exception as exc:
        logger.exception("Failed to update user metadata for user_id=%s", user_id)
        return {"status": "error", "error": f"Database error: {exc}"}

    if result is None:
        return {"status": "error", "error": "User not found"}

    return {
        "status": "ok",
        "user_id": str(user_id),
        "metadata": result,
    }
