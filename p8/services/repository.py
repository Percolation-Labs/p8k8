"""Generic repository — upsert, find, delete with auto-encryption."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from p8.ontology.base import CoreModel
from p8.services.database import Database
from p8.services.encryption import EncryptionService

T = TypeVar("T", bound=CoreModel)

# Columns stored as JSONB in postgres — need json.dumps() for asyncpg
_JSONB_COLUMNS = {"metadata", "graph_edges", "json_schema", "tool_calls", "auth_config",
                  "extracted_data", "present_persons", "parsed_output", "input_schema",
                  "output_schema"}


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


class Repository:
    def __init__(self, model_class: type[T], db: Database, encryption: EncryptionService):
        self.model_class = model_class
        self.table = model_class.__table_name__
        self.db = db
        self.encryption = encryption

    async def upsert(self, entities: CoreModel | list[CoreModel]) -> list[CoreModel]:
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
        rows_data = []
        for entity in entities:
            data = entity.model_dump(exclude_none=True)
            data = self.encryption.encrypt_fields(self.model_class, data, entity.tenant_id)
            data["encryption_level"] = tenant_modes.get(entity.tenant_id, "none") if entity.tenant_id else "none"
            rows_data.append({k: _jsonify(v) for k, v in data.items()})

        # Column set = union across all entities (preserves insertion order)
        columns = list(dict.fromkeys(col for row in rows_data for col in row))
        col_list = ", ".join(columns)

        # COALESCE preserves existing non-NULL values for partial updates
        updates = [
            f"{c} = COALESCE(EXCLUDED.{c}, {self.table}.{c})"
            for c in columns if c != "id"
        ]

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
        self, entity_id: UUID, *, tenant_id: str | None = None, decrypt: bool = True
    ) -> CoreModel | None:
        """Fetch entity by ID.

        decrypt=True  → decrypt encrypted fields (platform mode default)
        decrypt=False → return ciphertext as-is (client mode)
        """
        row = await self.db.fetchrow(
            f"SELECT * FROM {self.table} WHERE id = $1 AND deleted_at IS NULL", entity_id
        )
        if not row:
            return None
        return self._decrypt_row(row, tenant_id if decrypt else None)

    async def get_for_tenant(self, entity_id: UUID, *, tenant_id: str | None = None) -> CoreModel | None:
        """Mode-aware get: checks tenant mode to decide whether to decrypt."""
        should_decrypt = await self.encryption.should_decrypt_on_read(tenant_id)
        return await self.get(entity_id, tenant_id=tenant_id, decrypt=should_decrypt)

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
    ) -> list[CoreModel]:
        """List entities with optional filters.

        filters: extra column=value equality filters, e.g. {"kind": "agent", "name": "foo"}.
        """
        conditions = ["deleted_at IS NULL"]
        params: list = []
        idx = 1

        if tenant_id:
            conditions.append(f"tenant_id = ${idx}")
            params.append(tenant_id)
            idx += 1
        if user_id:
            conditions.append(f"user_id = ${idx}")
            params.append(user_id)
            idx += 1
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
        effective_tenant = tenant_id if decrypt else None
        return [self._decrypt_row(r, effective_tenant) for r in rows]

    async def find_for_tenant(self, *, tenant_id: str | None = None, **kwargs) -> list[CoreModel]:
        """Mode-aware find: checks tenant mode to decide whether to decrypt."""
        should_decrypt = await self.encryption.should_decrypt_on_read(tenant_id)
        return await self.find(tenant_id=tenant_id, decrypt=should_decrypt, **kwargs)

    async def delete(self, entity_id: UUID) -> bool:
        result = await self.db.execute(
            f"UPDATE {self.table} SET deleted_at = CURRENT_TIMESTAMP"
            f" WHERE id = $1 AND deleted_at IS NULL",
            entity_id,
        )
        return result == "UPDATE 1"

    def _decrypt_row(self, row, tenant_id: str | None) -> CoreModel:
        data = dict(row)
        # asyncpg may return JSONB as str when defaults come from DB
        for key in _JSONB_COLUMNS:
            if key in data and isinstance(data[key], str):
                data[key] = json.loads(data[key])
        if tenant_id:
            data = self.encryption.decrypt_fields(self.model_class, data, tenant_id)
        return self.model_class.model_validate(data)
