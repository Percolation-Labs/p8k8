"""CoreModel — base pydantic model with system fields for all p8 entities."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

# Stable namespace for deterministic UUID5 generation.
# All p8 entity IDs derived from natural keys use this namespace.
P8_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "p8.dev")


def deterministic_id(table: str, key: str, user_id: UUID | None = None) -> UUID:
    """Generate a deterministic UUID5 from table + natural key + user_id.

    Same inputs always produce the same UUID, enabling idempotent upserts
    via ON CONFLICT (id).
    """
    composite = f"{table}:{key}:{str(user_id) if user_id else ''}"
    return uuid.uuid5(P8_NAMESPACE, composite)


class CoreModel(BaseModel):
    """Base model for all p8 entities.

    Every entity table shares these system fields. graph_edges enables
    pseudo-graph relationships resolved via the KV store. metadata is
    extensible JSONB for ad-hoc fields that don't warrant a schema change.

    Class variables (overridden by subclasses):
      __encrypted_fields__ — fields encrypted at rest with tenant DEK
      __redacted_fields__  — fields that pass through PII pipeline before storage/embedding
      __id_fields__        — ordered field names used to derive deterministic ID
                             first non-None value wins; empty tuple → always uuid4
    """

    # --- Table name (set by subclasses) ---
    __table_name__: ClassVar[str]

    # --- Embedding field (set by subclasses, None = no embeddings) ---
    __embedding_field__: ClassVar[str | None] = None

    # --- Identity declarations (subclasses override) ---
    __id_fields__: ClassVar[tuple[str, ...]] = ("name",)
    # Ordered fields to try for deterministic ID generation.
    # First non-None value is used as the natural key.
    # Empty tuple → always uuid4 (no deterministic ID).

    # --- Privacy declarations (subclasses override) ---
    __encrypted_fields__: ClassVar[dict[str, str]] = {}
    # key = field name, value = "randomized" | "deterministic"

    __redacted_fields__: ClassVar[list[str]] = []
    # fields that get PII detection + redaction before storage

    # --- Instance fields ---
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    deleted_at: datetime | None = None

    tenant_id: str | None = None
    user_id: UUID | None = None
    encryption_level: str | None = None  # set by Repository on write: platform|client|sealed|disabled|none

    graph_edges: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    def model_post_init(self, __context: Any) -> None:
        """Generate deterministic ID from natural key fields when id is not explicitly set."""
        # If id was explicitly provided (e.g. loaded from DB), keep it
        if "id" in self.model_fields_set:
            return

        table = getattr(self.__class__, "__table_name__", None)
        if not table:
            return

        # Walk __id_fields__ in order, use first non-None value as key
        for field_name in self.__class__.__id_fields__:
            val = getattr(self, field_name, None)
            if val is not None:
                object.__setattr__(
                    self, "id", deterministic_id(table, str(val), self.user_id)
                )
                return
        # No key field found → keep the uuid4 default
