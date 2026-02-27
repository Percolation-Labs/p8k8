"""Tests for tenant-customizable dreamer resolution.

Verifies:
  - Default dreamer when no tenant config exists
  - Custom dreamers from tenant metadata
  - Fallback when tenant has no dreamer_agents set
  - Full pipeline with custom dreamer schema (LLM)
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from p8.ontology.types import Tenant, TenantMetadata
from p8.services.bootstrap import _export_api_keys
from p8.settings import Settings
from p8.api.tools import init_tools, set_tool_context
from p8.ontology.types import Moment
from p8.services.repository import Repository
from p8.workers.handlers.dreaming import DreamingHandler

from tests.integration.dreaming.fixtures import (
    TEST_USER_ID,
    setup_dreaming_fixtures,
)


TENANT_NAME = "test-dreaming-tenant"


class _Ctx:
    def __init__(self, db, encryption):
        self.db = db
        self.encryption = encryption


@pytest.fixture(autouse=True)
async def _clean(clean_db, db, encryption):
    # Clean tenants and tenant-related state
    await db.execute("DELETE FROM tenants WHERE name = $1", TENANT_NAME)
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'dream' AND user_id = $1",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1)",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM sessions WHERE mode IN ('dreaming', 'dream') AND user_id = $1",
        TEST_USER_ID,
    )
    # Ensure test user exists in users table (needed for tenant lookup)
    await db.execute(
        "INSERT INTO users (id, user_id, name, email) "
        "VALUES ($1, $1, 'Dream Test User', 'dreamer-test@example.com') "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        TEST_USER_ID,
    )
    # Clear adapter cache to avoid stale DB references across tests
    from p8.agentic.adapter import _adapter_cache
    _adapter_cache.clear()
    yield


async def test_resolve_default_no_tenant(db, encryption):
    """No tenant configured -> falls back to ["dreaming-agent"]."""
    handler = DreamingHandler()
    agents = await handler._resolve_dreamer_agents(db, TEST_USER_ID, tenant_id=None)
    assert agents == ["dreaming-agent"]


async def test_resolve_tenant_no_dreamer_config(db, encryption):
    """Tenant exists but no dreamer_agents in metadata -> falls back to default."""
    repo = Repository(Tenant, db, encryption)
    tenant = Tenant(name=TENANT_NAME, metadata={})
    await repo.upsert(tenant)

    # Link user to tenant
    await db.execute(
        "UPDATE users SET tenant_id = $1 WHERE (id = $2 OR user_id = $2)",
        TENANT_NAME, TEST_USER_ID,
    )
    try:
        handler = DreamingHandler()
        agents = await handler._resolve_dreamer_agents(db, TEST_USER_ID, tenant_id=None)
        assert agents == ["dreaming-agent"]
    finally:
        await db.execute(
            "UPDATE users SET tenant_id = NULL WHERE (id = $1 OR user_id = $1)",
            TEST_USER_ID,
        )


async def test_resolve_tenant_custom_dreamers(db, encryption):
    """Tenant with custom dreamer_agents -> returns custom list."""
    repo = Repository(Tenant, db, encryption)
    tenant = Tenant(
        name=TENANT_NAME,
        metadata={"dreamer_agents": ["my-dreamer", "creative-dreamer"]},
    )
    await repo.upsert(tenant)

    handler = DreamingHandler()
    agents = await handler._resolve_dreamer_agents(db, TEST_USER_ID, tenant_id=TENANT_NAME)
    assert agents == ["my-dreamer", "creative-dreamer"]


async def test_resolve_tenant_via_user_lookup(db, encryption):
    """When tenant_id not in task, resolve via user's tenant_id column."""
    repo = Repository(Tenant, db, encryption)
    tenant = Tenant(
        name=TENANT_NAME,
        metadata={"dreamer_agents": ["tenant-dreamer"]},
    )
    await repo.upsert(tenant)

    await db.execute(
        "UPDATE users SET tenant_id = $1 WHERE (id = $2 OR user_id = $2)",
        TENANT_NAME, TEST_USER_ID,
    )
    try:
        handler = DreamingHandler()
        # Pass tenant_id=None -- handler should resolve it from user
        agents = await handler._resolve_dreamer_agents(db, TEST_USER_ID, tenant_id=None)
        assert agents == ["tenant-dreamer"]
    finally:
        await db.execute(
            "UPDATE users SET tenant_id = NULL WHERE (id = $1 OR user_id = $1)",
            TEST_USER_ID,
        )


async def test_tenant_metadata_model():
    """TenantMetadata parses correctly from raw dict."""
    raw = {"dreamer_agents": ["a", "b"]}
    meta = TenantMetadata(**raw)
    assert meta.dreamer_agents == ["a", "b"]

    # Empty metadata
    meta_empty = TenantMetadata()
    assert meta_empty.dreamer_agents is None

    # Partial metadata (other keys ignored)
    meta_partial = TenantMetadata(**{"some_other_key": True})
    assert meta_partial.dreamer_agents is None


@pytest.mark.llm
async def test_full_pipeline_with_tenant_dreamer(db, encryption):
    """Full E2E: tenant with custom dreamer -> runs that agent, produces dreams.

    Uses the built-in dreaming-agent (referenced by a different name via tenant config)
    to verify the pipeline works end-to-end with tenant resolution.
    """
    _export_api_keys(Settings())
    init_tools(db, encryption)
    set_tool_context(user_id=TEST_USER_ID)
    await setup_dreaming_fixtures(db, encryption)

    # Set up tenant pointing to the built-in dreaming-agent
    repo = Repository(Tenant, db, encryption)
    tenant = Tenant(
        name=TENANT_NAME,
        metadata={"dreamer_agents": ["dreaming-agent"]},
    )
    await repo.upsert(tenant)

    handler = DreamingHandler()
    ctx = _Ctx(db, encryption)

    result = await handler.handle(
        {"user_id": str(TEST_USER_ID), "lookback_days": 1, "tenant_id": TENANT_NAME},
        ctx,
    )

    phase2 = result.get("phase2", {})
    assert phase2.get("status") == "ok", f"Phase 2 failed: {phase2}"
    assert phase2.get("agents_run") == ["dreaming-agent"]
    assert phase2.get("moments_saved", 0) >= 1

    # Verify dream moments were created
    moment_repo = Repository(Moment, db, encryption)
    dreams = await moment_repo.find(
        user_id=TEST_USER_ID,
        filters={"moment_type": "dream"},
    )
    assert len(dreams) >= 1

    # Verify quality (from quality test criteria)
    for dream in dreams:
        summary = dream.summary or ""
        assert "##" in summary, f"Dream {dream.name} missing ## heading: {summary[:100]}"
        assert "moment://" in summary or "resource://" in summary, (
            f"Dream {dream.name} missing internal links: {summary[:100]}"
        )

    print(f"\nTenant dreamer test: {len(dreams)} dreams created via tenant config")
    for d in dreams:
        print(f"  {d.name}: {(d.summary or '')[:120]}...")
