"""Integration tests for ontology/verify.py — require a running Postgres."""

from __future__ import annotations

import pytest
import pytest_asyncio

from p8.ontology.types import ALL_ENTITY_TYPES
from p8.ontology.verify import verify_all, register_models


@pytest_asyncio.fixture(autouse=True)
async def _clean(clean_db):
    yield


class TestVerifyIntegration:
    """Integration tests — require a running postgres with p8 schema."""

    @pytest.mark.asyncio
    async def test_all_models_pass_on_clean_db(self, db):
        """All core models should verify clean against a freshly migrated DB."""
        issues = await verify_all(db)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            for e in errors:
                print(f"  [{e.level}] {e.table}: {e.check} — {e.message}")
        assert len(errors) == 0

    @pytest.mark.asyncio
    async def test_register_models_idempotent(self, db):
        """register_models should upsert all 13 models without error."""
        count = await register_models(db)
        assert count == len(ALL_ENTITY_TYPES)

        # Running again should produce the same count (idempotent)
        count2 = await register_models(db)
        assert count2 == count

    @pytest.mark.asyncio
    async def test_register_then_verify(self, db):
        """After register_models, verify should report 0 schema metadata errors."""
        await register_models(db)
        issues = await verify_all(db)
        metadata_errors = [i for i in issues if i.check == "schema_metadata_mismatch"]
        assert len(metadata_errors) == 0
