"""DDL verification and model registration engine.

Compares pydantic model declarations against live database state and
syncs model metadata into the schemas table (kind='table') from the
Python source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from p8.ontology.base import CoreModel
from p8.ontology.types import ALL_ENTITY_TYPES, KV_TABLES
from p8.utils.parsing import ensure_parsed


# ---------------------------------------------------------------------------
# Issue — a single verification finding
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    table: str
    level: str  # "error" | "warning"
    check: str  # machine-readable check name
    message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# CoreModel system fields — present on every entity table via SQL DDL,
# not worth checking individually per model.
_CORE_FIELDS = {f for f in CoreModel.model_fields}


def _model_columns(model: type[CoreModel]) -> set[str]:
    """Return the set of column names a model declares (excluding CoreModel system fields)."""
    return set(model.model_fields) - _CORE_FIELDS


def _expected_triggers(table: str, has_kv: bool, has_embed: bool) -> list[str]:
    """Return trigger names expected for a table based on its config."""
    triggers = [f"trg_{table}_updated_at"]
    if has_kv:
        triggers.append(f"trg_{table}_kv")
    if has_embed:
        triggers.append(f"trg_{table}_embed")
    # schemas table also gets timemachine trigger
    if table == "schemas":
        triggers.append("trg_schemas_timemachine")
    return triggers


def _derive_kv_summary(model: type[CoreModel]) -> str | None:
    """Derive the kv_summary_expr from model field declarations.

    Follows the same logic as seed_table_schemas() in install_entities.sql:
    - Encrypted content → "name" (KV stores name only, not ciphertext)
    - Has content + description + name → COALESCE(content, description, name)
    - Has description + name → COALESCE(description, name)
    - Has name only → "name"
    - None of the above → None (no KV sync)
    """
    table = model.__table_name__
    if table not in KV_TABLES:
        return None

    fields = set(model.model_fields)
    encrypted = getattr(model, "__encrypted_fields__", {})

    has_content = "content" in fields
    has_description = "description" in fields
    has_name = "name" in fields
    content_encrypted = "content" in encrypted

    # Encrypted content — use name only for KV summary
    if has_content and content_encrypted:
        return "name"

    # schemas-like: content + description + name
    if has_content and has_description and has_name:
        return "COALESCE(content, description, name)"

    # description + name
    if has_description and has_name:
        return "COALESCE(description, name)"

    # name only
    if has_name:
        return "name"

    return None


def _build_json_schema(model: type[CoreModel]) -> dict:
    """Build the json_schema metadata dict from model class variables."""
    table = model.__table_name__
    embedding_field = getattr(model, "__embedding_field__", None)
    encrypted_fields = getattr(model, "__encrypted_fields__", {})

    return {
        "has_kv_sync": table in KV_TABLES,
        "has_embeddings": embedding_field is not None,
        "embedding_field": embedding_field,
        "is_encrypted": bool(encrypted_fields),
        "kv_summary_expr": _derive_kv_summary(model),
    }


# ---------------------------------------------------------------------------
# verify_model — check one model against live DB
# ---------------------------------------------------------------------------


async def verify_model(model: type[CoreModel], db) -> list[Issue]:
    """Verify a single model's declarations against the live database."""
    issues: list[Issue] = []
    table = model.__table_name__
    embedding_field = getattr(model, "__embedding_field__", None)

    # 1. Table exists
    exists = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = 'public' AND table_name = $1)",
        table,
    )
    if not exists:
        issues.append(Issue(table, "error", "missing_table", f"Table '{table}' does not exist"))
        return issues  # no point checking columns/triggers if table doesn't exist

    # 2. Columns present
    rows = await db.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    db_columns = {r["column_name"] for r in rows}
    model_cols = _model_columns(model)

    for col in sorted(model_cols - db_columns):
        issues.append(Issue(table, "error", "missing_column", f"Column '{col}' declared in model but missing from DB"))
    for col in sorted(db_columns - model_cols - _CORE_FIELDS - {"embedding"}):
        # Extra DB columns are warnings — may be legacy or hand-added
        issues.append(Issue(table, "warning", "extra_column", f"Column '{col}' exists in DB but not declared in model"))

    # 3. Embedding table
    embed_table = f"embeddings_{table}"
    embed_exists = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = 'public' AND table_name = $1)",
        embed_table,
    )
    if embedding_field is not None and not embed_exists:
        issues.append(Issue(table, "error", "missing_embedding_table", f"Embedding table '{embed_table}' expected but missing"))
    if embedding_field is None and embed_exists:
        issues.append(Issue(table, "warning", "stale_embedding_table", f"Embedding table '{embed_table}' exists but model has no __embedding_field__"))

    # 4. Schema row registered
    schema_row = await db.fetchrow(
        "SELECT json_schema FROM schemas WHERE name = $1 AND kind = 'table'",
        table,
    )
    if schema_row is None:
        issues.append(Issue(table, "error", "unregistered_schema", f"No schemas row with name='{table}' kind='table'"))
    else:
        # 5. Schema metadata matches
        db_meta = ensure_parsed(schema_row["json_schema"], default={})
        expected = _build_json_schema(model)
        for key, expected_val in expected.items():
            db_val = db_meta.get(key)
            if db_val != expected_val:
                issues.append(
                    Issue(
                        table,
                        "error",
                        "schema_metadata_mismatch",
                        f"json_schema.{key}: expected {expected_val!r}, got {db_val!r}",
                    )
                )

    # 6. Triggers installed
    has_kv = table in KV_TABLES
    has_embed = embedding_field is not None
    expected_triggers = _expected_triggers(table, has_kv, has_embed)

    trigger_rows = await db.fetch(
        "SELECT trigger_name FROM information_schema.triggers"
        " WHERE event_object_schema = 'public' AND event_object_table = $1",
        table,
    )
    # triggers may appear multiple times (once per event), so deduplicate
    db_triggers = {r["trigger_name"] for r in trigger_rows}

    for trig in expected_triggers:
        if trig not in db_triggers:
            issues.append(Issue(table, "error", "missing_trigger", f"Trigger '{trig}' expected but not installed"))

    return issues


# ---------------------------------------------------------------------------
# verify_all — check every model
# ---------------------------------------------------------------------------


async def verify_all(db) -> list[Issue]:
    """Run verification for all entity types in ALL_ENTITY_TYPES."""
    all_issues: list[Issue] = []
    for model in ALL_ENTITY_TYPES:
        all_issues.extend(await verify_model(model, db))
    return all_issues


# ---------------------------------------------------------------------------
# register_models — upsert schema rows from Python source of truth
# ---------------------------------------------------------------------------


async def register_models(db) -> int:
    """Upsert schemas rows (kind='table') for all entity models.

    Returns the number of rows upserted.
    """
    count = 0
    for model in ALL_ENTITY_TYPES:
        table = model.__table_name__
        meta = _build_json_schema(model)

        # Derive description from model docstring
        description = (model.__doc__ or "").strip().split("\n")[0] or table

        # Pass dict directly — pool's JSONB codec handles serialization
        await db.execute(
            "INSERT INTO schemas (name, kind, description, json_schema)"
            " VALUES ($1, 'table', $2, $3)"
            " ON CONFLICT (name) DO UPDATE SET"
            "   kind = 'table',"
            "   description = EXCLUDED.description,"
            "   json_schema = EXCLUDED.json_schema",
            table,
            description,
            meta,
        )
        count += 1

    return count
