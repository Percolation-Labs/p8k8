"""CRUD for ontology schemas."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from p8.api.deps import get_db, get_encryption
from p8.ontology.types import TABLE_MAP, Schema
from p8.ontology.verify import verify_model
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.repository import Repository

router = APIRouter()


@router.post("/", status_code=201)
async def upsert_schema(
    schema: Schema,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    repo = Repository(Schema, db, encryption)
    [result] = await repo.upsert(schema)

    response: dict = result.model_dump(mode="json")

    # Run DDL verification for kind='table' schemas
    if result.kind == "table" and result.name in TABLE_MAP:
        issues = await verify_model(TABLE_MAP[result.name], db)
        if issues:
            response["_verify"] = [
                {"level": i.level, "check": i.check, "message": i.message}
                for i in issues
            ]

    return response


@router.get("/{schema_id}")
async def get_schema(
    schema_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    repo = Repository(Schema, db, encryption)
    result = await repo.get(schema_id)
    if not result:
        raise HTTPException(404, "Schema not found")
    return result.model_dump(mode="json")


@router.get("/")
async def list_schemas(
    kind: str | None = None,
    limit: int = 50,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    repo = Repository(Schema, db, encryption)
    filters = {"kind": kind} if kind else None
    results = await repo.find(filters=filters, limit=limit)
    return [r.model_dump(mode="json") for r in results]


@router.delete("/{schema_id}")
async def delete_schema(
    schema_id: UUID,
    db: Database = Depends(get_db),
    encryption: EncryptionService = Depends(get_encryption),
):
    repo = Repository(Schema, db, encryption)
    deleted = await repo.delete(schema_id)
    if not deleted:
        raise HTTPException(404, "Schema not found")
    return {"deleted": True}
