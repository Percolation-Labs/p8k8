"""Generic repository — upsert, find, delete with auto-encryption."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Generic, TypeVar
from uuid import UUID

from p8.ontology.base import CoreModel
from p8.services.database import Database
from p8.services.encryption import EncryptionService

if TYPE_CHECKING:
    from p8.api.security import SecurityContext

T = TypeVar("T", bound=CoreModel)

# Columns stored as JSONB in postgres — need json.dumps() for asyncpg
_JSONB_COLUMNS = {"metadata", "graph_edges", "json_schema", "tool_calls", "auth_config",
                  "extracted_data", "present_persons", "parsed_output", "input_schema",
                  "output_schema", "devices"}

# CoreModel fields with empty-collection defaults (dict/list).
# When these aren't explicitly set by the caller and remain at their default,
# we strip them from the upsert so COALESCE preserves the existing DB value.
_EMPTY_DEFAULT_FIELDS = {"metadata", "graph_edges", "tags"}


def _prepare_value(key: str, value):
    """Convert Python types to asyncpg-compatible values."""
    if key in _JSONB_COLUMNS and isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _jsonify(value):
    """Convert Python types to JSON-serializable values for jsonb_populate_recordset."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class Repository(Generic[T]):
    def __init__(self, model_class: type[T], db: Database, encryption: EncryptionService):
        self.model_class = model_class
        self.table = model_class.__table_name__
        self.db = db
        self.encryption = encryption

    async def upsert(self, entities: T | list[T]) -> list[T]:
        """Bulk insert-or-update via jsonb_populate_recordset.

        Accepts a single entity or list. Always returns a list.
        Uses PG-native jsonb_populate_recordset(NULL::table, $1::jsonb)
        for single-parameter bulk operation with automatic type coercion.
        """
        if isinstance(entities, CoreModel):
            entities = [entities]
        if not entities:
            return []

        # Ensure DEKs cached and resolve encryption modes for all tenants
        tenant_modes: dict[str, str] = {}
        for tid in {e.tenant_id for e in entities if e.tenant_id}:
            await self.encryption.get_dek(tid)
            tenant_modes[tid] = await self.encryption.get_tenant_mode(tid)

        # Dump, encrypt, stamp encryption_level, and convert to JSON-serializable dicts
        # Track which fields were implicitly empty defaults (not caller-set) so
        # the ON CONFLICT clause can preserve existing DB values for those fields.
        _unset_empty: set[str] = set()
        rows_data = []
        for entity in entities:
            data = entity.model_dump(exclude_none=True)
            for field_name in _EMPTY_DEFAULT_FIELDS:
                if field_name not in entity.model_fields_set and field_name in data:
                    val = data[field_name]
                    if isinstance(val, (dict, list)) and not val:
                        _unset_empty.add(field_name)
            data = self.encryption.encrypt_fields(self.model_class, data, entity.tenant_id)
            data["encryption_level"] = tenant_modes.get(entity.tenant_id, "none") if entity.tenant_id else "none"
            rows_data.append({k: _jsonify(v) for k, v in data.items()})

        # Column set = union across all entities (preserves insertion order)
        columns = list(dict.fromkeys(col for row in rows_data for col in row))
        col_list = ", ".join(columns)

        # COALESCE preserves existing non-NULL values for partial updates.
        # For fields in _unset_empty, prefer the existing DB value over the
        # empty default so metadata/tags/graph_edges aren't wiped on upsert.
        updates = []
        for c in columns:
            if c == "id":
                continue
            if c in _unset_empty:
                updates.append(f"{c} = COALESCE({self.table}.{c}, EXCLUDED.{c})")
            else:
                updates.append(f"{c} = COALESCE(EXCLUDED.{c}, {self.table}.{c})")

        sql = (
            f"INSERT INTO {self.table} ({col_list})"
            f" SELECT {col_list}"
            f" FROM jsonb_populate_recordset(NULL::{self.table}, $1::jsonb)"
            f" ON CONFLICT (id) DO UPDATE SET {', '.join(updates)}"
            f" RETURNING *"
        )

        # asyncpg JSONB codec auto-serializes Python list → JSON array
        result_rows = await self.db.fetch(sql, rows_data)

        # Map returned rows back to tenant_ids for decryption
        tenant_map = {str(e.id): e.tenant_id for e in entities}
        return [
            self._decrypt_row(row, tenant_map.get(str(row["id"])))
            for row in result_rows
        ]

    async def get(
        self,
        entity_id: UUID,
        *,
        tenant_id: str | None = None,
        decrypt: bool = True,
        security: SecurityContext | None = None,
    ) -> T | None:
        """Fetch entity by ID. Auto-decrypts platform-encrypted rows.

        When ``security`` is provided, a post-fetch access check is performed.
        """
        row = await self.db.fetchrow(
            f"SELECT * FROM {self.table} WHERE id = $1 AND deleted_at IS NULL", entity_id
        )
        if not row:
            return None
        if security and not security.can_access_record(row.get("user_id"), row.get("tenant_id")):
            return None
        if decrypt:
            await self._ensure_deks([row], tenant_id)
        return self._decrypt_row(row, tenant_id if decrypt else None)

    async def get_for_tenant(self, entity_id: UUID, *, tenant_id: str | None = None) -> T | None:
        """Mode-aware get — delegates to get() which auto-decrypts platform rows."""
        return await self.get(entity_id, tenant_id=tenant_id)

    async def find(
        self,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
        tags: list[str] | None = None,
        filters: dict[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
        decrypt: bool = True,
        security: SecurityContext | None = None,
    ) -> list[T]:
        """List entities with optional filters.

        filters: extra column=value equality filters, e.g. {"kind": "agent", "name": "foo"}.

        When ``security`` is provided, effective_tenant_id / effective_user_id
        are injected as WHERE clauses.  At USER level the filter is
        ``(user_id = $N OR user_id IS NULL)`` to include shared records.
        Explicit tenant_id / user_id params still take precedence.
        """
        conditions = ["deleted_at IS NULL"]
        params: list = []
        idx = 1

        # Security-derived filters (only when not overridden by explicit params)
        eff_tenant = tenant_id
        eff_user = user_id
        if security:
            if not eff_tenant and security.effective_tenant_id:
                eff_tenant = security.effective_tenant_id
            if not eff_user and security.effective_user_id:
                eff_user = security.effective_user_id

        if eff_tenant:
            conditions.append(f"tenant_id = ${idx}")
            params.append(eff_tenant)
            idx += 1
        if eff_user:
            # Include user's own records + shared (user_id IS NULL)
            conditions.append(f"(user_id = ${idx} OR user_id IS NULL)")
            params.append(eff_user)
            idx += 1
        elif user_id is not None:
            # Explicit user_id=None was not passed, keep original behavior
            pass
        if tags:
            conditions.append(f"tags @> ${idx}")
            params.append(tags)
            idx += 1
        for col, val in (filters or {}).items():
            conditions.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = await self.db.fetch(
            f"SELECT * FROM {self.table} WHERE {where}"
            f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params,
        )
        if decrypt:
            await self._ensure_deks(rows, eff_tenant or tenant_id)
        effective_tenant = (eff_tenant or tenant_id) if decrypt else None
        return [self._decrypt_row(r, effective_tenant) for r in rows]

    async def find_for_tenant(self, *, tenant_id: str | None = None, **kwargs) -> list[T]:
        """Mode-aware find — delegates to find() which auto-decrypts platform rows."""
        return await self.find(tenant_id=tenant_id, **kwargs)

    async def merge_metadata(
        self,
        entity_id: UUID,
        patch: dict,
        *,
        remove_keys: list[str] | None = None,
        security: SecurityContext | None = None,
    ) -> dict | None:
        """Atomic shallow-merge into the metadata JSONB column.

        Keys in ``patch`` overwrite existing keys; absent keys are preserved.
        ``remove_keys`` deletes top-level keys after the merge.

        When ``security`` is provided, a pre-fetch authorization check ensures
        the caller can access the target record.

        Returns the merged metadata dict, or None if entity not found.
        """
        # Pre-fetch authorization check
        if security:
            row = await self.db.fetchrow(
                f"SELECT user_id, tenant_id FROM {self.table}"
                f" WHERE id = $1 AND deleted_at IS NULL",
                entity_id,
            )
            if not row or not security.can_access_record(row.get("user_id"), row.get("tenant_id")):
                return None

        remove_keys = remove_keys or []

        if remove_keys:
            expr = "(COALESCE(metadata, '{}'::jsonb) || ($1::text)::jsonb)"
            for i, _key in enumerate(remove_keys):
                expr += f" - ${i + 3}"
            sql = (
                f"UPDATE {self.table} SET metadata = {expr}, "
                f"updated_at = CURRENT_TIMESTAMP "
                f"WHERE id = $2 AND deleted_at IS NULL "
                f"RETURNING metadata"
            )
            params: list = [json.dumps(patch), entity_id, *remove_keys]
        else:
            sql = (
                f"UPDATE {self.table} SET metadata = "
                f"COALESCE(metadata, '{{}}'::jsonb) || ($1::text)::jsonb, "
                f"updated_at = CURRENT_TIMESTAMP "
                f"WHERE id = $2 AND deleted_at IS NULL "
                f"RETURNING metadata"
            )
            params = [json.dumps(patch), entity_id]

        row = await self.db.fetchrow(sql, *params)
        if not row:
            return None
        result: dict = row["metadata"]
        if isinstance(result, str):
            result = json.loads(result)
        return result

    async def delete(
        self, entity_id: UUID, *, security: SecurityContext | None = None
    ) -> bool:
        """Soft-delete entity by UUID.

        When ``security`` is provided, a pre-fetch authorization check ensures
        the caller can access the target record before deleting.
        """
        if security:
            row = await self.db.fetchrow(
                f"SELECT user_id, tenant_id FROM {self.table}"
                f" WHERE id = $1 AND deleted_at IS NULL",
                entity_id,
            )
            if not row or not security.can_access_record(row.get("user_id"), row.get("tenant_id")):
                return False

        result = await self.db.execute(
            f"UPDATE {self.table} SET deleted_at = CURRENT_TIMESTAMP"
            f" WHERE id = $1 AND deleted_at IS NULL",
            entity_id,
        )
        return bool(result == "UPDATE 1")

    async def delete_by_name(self, name: str, *, user_id: UUID | None = None) -> bool:
        """Soft-delete entity by name (optionally scoped to user)."""
        if user_id:
            result = await self.db.execute(
                f"UPDATE {self.table} SET deleted_at = CURRENT_TIMESTAMP"
                f" WHERE name = $1 AND user_id = $2 AND deleted_at IS NULL",
                name, user_id,
            )
        else:
            result = await self.db.execute(
                f"UPDATE {self.table} SET deleted_at = CURRENT_TIMESTAMP"
                f" WHERE name = $1 AND deleted_at IS NULL",
                name,
            )
        return bool(result and result.endswith("1"))

    async def _ensure_deks(self, rows, fallback_tenant: str | None = None) -> None:
        """Pre-load DEKs for platform-encrypted rows so _decrypt_row can work sync."""
        tenants: set[str] = set()
        for row in rows:
            data = dict(row)
            if data.get("encryption_level") == "platform":
                tid = fallback_tenant or data.get("tenant_id")
                if tid:
                    tenants.add(tid)
        for tid in tenants:
            await self.encryption.get_dek(tid)

    def _decrypt_row(self, row, tenant_id: str | None = None) -> T:
        data = dict(row)
        # asyncpg may return JSONB as str when defaults come from DB
        for key in _JSONB_COLUMNS:
            if key in data and isinstance(data[key], str):
                data[key] = json.loads(data[key])

        # Auto-decrypt: use row's encryption_level to decide
        effective_tenant = tenant_id or data.get("tenant_id")
        level = data.get("encryption_level")
        if effective_tenant and (
            level == "platform"                           # stamped at write time
            or (level is None and tenant_id)              # legacy: caller passed tenant_id
        ):
            data = self.encryption.decrypt_fields(self.model_class, data, effective_tenant)

        return self.model_class.model_validate(data)
