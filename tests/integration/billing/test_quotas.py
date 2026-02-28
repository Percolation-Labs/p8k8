"""Integration tests for the quota system — SQL function, service layer, API enforcement."""

from __future__ import annotations

import io
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from p8.services.usage import (
    GB,
    MB,
    QuotaStatus,
    _plan_cache,
    check_quota,
    get_limits,
    get_user_plan,
    increment_usage,
)


# ── Helpers ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_plan_cache():
    """Ensure the in-memory plan cache is empty before each test."""
    _plan_cache.clear()
    yield
    _plan_cache.clear()


def _sql_seed(client: TestClient, sql: str) -> None:
    """Seed data via the /query/ endpoint (runs inside the app's async loop)."""
    resp = client.post("/query/", json={"mode": "SQL", "query": sql})
    assert resp.status_code == 200, f"SQL seed failed: {resp.text}"


# ── 1. SQL function: usage_increment() basics ────────────────────────────

@pytest.mark.asyncio
async def test_usage_increment_fresh_row(db):
    """First increment for a user creates a new row and returns correct values."""
    uid = uuid4()
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 500, 50_000,
    )
    assert row["new_used"] == 500
    assert row["effective_limit"] == 50_000
    assert row["exceeded"] is False


@pytest.mark.asyncio
async def test_usage_increment_accumulates(db):
    """Successive increments accumulate the used counter."""
    uid = uuid4()
    await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 1000, 50_000,
    )
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 2000, 50_000,
    )
    assert row["new_used"] == 3000
    assert row["exceeded"] is False


@pytest.mark.asyncio
async def test_usage_increment_exceed_limit(db):
    """Exceeding the limit sets exceeded = true."""
    uid = uuid4()
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 30_000, 25_000,
    )
    assert row["new_used"] == 30_000
    assert row["exceeded"] is True


# ── 2. SQL function: usage_increment() with granted_extra ────────────────

@pytest.mark.asyncio
async def test_usage_increment_granted_extra_extends_limit(db):
    """granted_extra extends the effective limit beyond the base plan limit."""
    uid = uuid4()
    # Pre-seed a row with granted_extra
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used, granted_extra) "
        "VALUES ($1, $2, date_trunc('month', CURRENT_DATE)::date, 0, 10000)",
        uid, "chat_tokens",
    )
    # Increment to base limit — should NOT exceed because extra extends it
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 25_000, 25_000,
    )
    assert row["new_used"] == 25_000
    assert row["effective_limit"] == 35_000  # 25000 + 10000
    assert row["exceeded"] is False


@pytest.mark.asyncio
async def test_usage_increment_exceed_with_granted_extra(db):
    """Exceeding base + extra limit sets exceeded = true."""
    uid = uuid4()
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used, granted_extra) "
        "VALUES ($1, $2, date_trunc('month', CURRENT_DATE)::date, 0, 10000)",
        uid, "chat_tokens",
    )
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
        uid, "chat_tokens", 36_000, 25_000,
    )
    assert row["new_used"] == 36_000
    assert row["effective_limit"] == 35_000
    assert row["exceeded"] is True


# ── 3. Service: get_user_plan() defaults ─────────────────────────────────

@pytest.mark.asyncio
async def test_get_user_plan_defaults_to_free(db):
    """No stripe_customers row → returns 'free'."""
    uid = uuid4()
    plan = await get_user_plan(db, uid)
    assert plan == "free"


@pytest.mark.asyncio
async def test_get_user_plan_returns_stripe_plan(db):
    """stripe_customers row with plan_id='pro' → returns 'pro'."""
    uid = uuid4()
    await db.execute(
        "INSERT INTO stripe_customers (user_id, stripe_customer_id, plan_id) "
        "VALUES ($1, $2, $3)",
        uid, f"cus_test_{uid.hex[:16]}", "pro",
    )
    plan = await get_user_plan(db, uid)
    assert plan == "pro"


@pytest.mark.asyncio
async def test_get_user_plan_cache_invalidation(db):
    """Clearing _plan_cache allows picking up a changed plan."""
    uid = uuid4()
    # First call — no row → free
    assert await get_user_plan(db, uid) == "free"

    # Insert pro plan
    await db.execute(
        "INSERT INTO stripe_customers (user_id, stripe_customer_id, plan_id) "
        "VALUES ($1, $2, $3)",
        uid, f"cus_test_{uid.hex[:16]}", "pro",
    )
    # Still cached as free
    assert await get_user_plan(db, uid) == "free"

    # Clear cache → picks up pro
    _plan_cache.clear()
    assert await get_user_plan(db, uid) == "pro"


# ── 4. Service: check_quota() for periodic resources ─────────────────────

@pytest.mark.asyncio
async def test_check_quota_no_usage(db):
    """No usage row → QuotaStatus(used=0, limit=free chat_tokens, exceeded=False)."""
    uid = uuid4()
    status = await check_quota(db, uid, "chat_tokens", "free")
    assert status == QuotaStatus(used=0, limit=50_000, exceeded=False)


@pytest.mark.asyncio
async def test_check_quota_after_increment(db):
    """After incrementing usage, check_quota reflects the new used value."""
    uid = uuid4()
    await increment_usage(db, uid, "chat_tokens", 5000, "free")
    status = await check_quota(db, uid, "chat_tokens", "free")
    assert status.used == 5000
    assert status.limit == 50_000
    assert status.exceeded is False


@pytest.mark.asyncio
async def test_check_quota_exceeded(db):
    """Incrementing past the limit → exceeded=True."""
    uid = uuid4()
    await increment_usage(db, uid, "chat_tokens", 101_000, "free")
    status = await check_quota(db, uid, "chat_tokens", "free")
    assert status.used == 101_000
    assert status.exceeded is True


# ── 5. Service: check_quota() for storage_bytes ──────────────────────────

@pytest.mark.asyncio
async def test_check_quota_storage_bytes(db):
    """storage_bytes quota computed from files table."""
    uid = uuid4()
    # Insert two files
    for i in range(2):
        await db.execute(
            "INSERT INTO files (id, name, size_bytes, user_id) VALUES ($1, $2, $3, $4)",
            uuid4(), f"file_{i}.txt", 5 * GB, uid,
        )
    status = await check_quota(db, uid, "storage_bytes", "free")
    assert status.used == 10 * GB
    assert status.limit == 40 * GB
    assert status.exceeded is False


@pytest.mark.asyncio
async def test_check_quota_storage_bytes_exceeded(db):
    """Exceeding 40*GB free plan storage → exceeded."""
    uid = uuid4()
    await db.execute(
        "INSERT INTO files (id, name, size_bytes, user_id) VALUES ($1, $2, $3, $4)",
        uuid4(), "big.bin", 41 * GB, uid,
    )
    status = await check_quota(db, uid, "storage_bytes", "free")
    assert status.used == 41 * GB
    assert status.exceeded is True


@pytest.mark.asyncio
async def test_check_quota_storage_ignores_deleted(db):
    """Soft-deleted files should not count toward storage."""
    uid = uuid4()
    await db.execute(
        "INSERT INTO files (id, name, size_bytes, user_id, deleted_at) "
        "VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)",
        uuid4(), "deleted.bin", 20 * GB, uid,
    )
    status = await check_quota(db, uid, "storage_bytes", "free")
    assert status.used == 0
    assert status.exceeded is False


# ── 6. Monthly period isolation ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_monthly_period_isolation(db):
    """Usage from a past month does not affect the current month."""
    uid = uuid4()
    # Seed usage for a past month
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used) "
        "VALUES ($1, $2, '2025-01-01', 20000)",
        uid, "chat_tokens",
    )
    # Current month should show 0
    status = await check_quota(db, uid, "chat_tokens", "free")
    assert status.used == 0
    assert status.exceeded is False

    # Increment in current month — only current month affected
    await increment_usage(db, uid, "chat_tokens", 3000, "free")
    status = await check_quota(db, uid, "chat_tokens", "free")
    assert status.used == 3000

    # Past month still untouched
    row = await db.fetchrow(
        "SELECT used FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = $2 AND period_start = '2025-01-01'",
        uid, "chat_tokens",
    )
    assert row["used"] == 20000


# ── 7. API: chat endpoint returns 429 on quota exceeded ──────────────────

@pytest.fixture(scope="module")
def client():
    from p8.api.main import app

    with TestClient(app) as c:
        yield c


def test_chat_429_when_quota_exceeded(client):
    """POST /chat/ with exceeded quota → 429 chat_token_quota_exceeded."""
    uid = uuid4()
    _sql_seed(
        client,
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used) "
        f"VALUES ('{uid}', 'chat_tokens', date_trunc('day', CURRENT_DATE)::date, 51000)",
    )
    _plan_cache.clear()

    msg_id = str(uuid4())
    resp = client.post(
        f"/chat/{uuid4()}",
        json={"messages": [{"role": "user", "content": "hello", "id": msg_id}]},
        headers={"x-user-id": str(uid)},
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "chat_token_quota_exceeded"
    assert detail["used"] == 51000
    assert detail["limit"] == 50_000


# ── 8. API: content upload returns 429 on storage exceeded ────────────────

def test_content_upload_429_when_storage_exceeded(client):
    """POST /content/ with exceeded storage quota → 429 storage_quota_exceeded."""
    uid = uuid4()
    fid = uuid4()
    _sql_seed(
        client,
        "INSERT INTO files (id, name, size_bytes, user_id) "
        f"VALUES ('{fid}', 'huge.bin', {41 * GB}, '{uid}')",
    )
    _plan_cache.clear()

    resp = client.post(
        "/content/",
        files={"file": ("test.txt", io.BytesIO(b"hello"), "text/plain")},
        headers={"x-user-id": str(uid)},
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "storage_quota_exceeded"


# ── 9. Plan upgrade raises limits ────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_upgrade_raises_limits(db):
    """Upgrading from free to pro increases the quota limit."""
    uid = uuid4()
    # Free plan
    status_free = await check_quota(db, uid, "chat_tokens", "free")
    assert status_free.limit == 50_000

    # Pro plan
    status_pro = await check_quota(db, uid, "chat_tokens", "pro")
    assert status_pro.limit == 200_000

    # Verify via get_limits too
    assert get_limits("free").chat_tokens == 50_000
    assert get_limits("pro").chat_tokens == 200_000
    assert get_limits("unknown_plan").chat_tokens == 50_000  # defaults to free


# ── 10. Dreaming quota tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dreaming_io_tokens_quota(db):
    """dreaming_io_tokens quota: free plan limit=40000, tracks usage correctly."""
    uid = uuid4()

    # Fresh — no usage
    status = await check_quota(db, uid, "dreaming_io_tokens", "free")
    assert status.limit == 40_000
    assert status.used == 0
    assert status.exceeded is False

    # Increment below limit
    await increment_usage(db, uid, "dreaming_io_tokens", 20_000, "free")
    status = await check_quota(db, uid, "dreaming_io_tokens", "free")
    assert status.used == 20_000
    assert status.exceeded is False

    # Increment past limit
    await increment_usage(db, uid, "dreaming_io_tokens", 21_000, "free")
    status = await check_quota(db, uid, "dreaming_io_tokens", "free")
    assert status.used == 41_000
    assert status.exceeded is True


@pytest.mark.asyncio
async def test_dreaming_minutes_quota(db):
    """dreaming_minutes quota: free plan limit=120, tracks usage correctly."""
    uid = uuid4()

    status = await check_quota(db, uid, "dreaming_minutes", "free")
    assert status.limit == 120
    assert status.used == 0
    assert status.exceeded is False

    await increment_usage(db, uid, "dreaming_minutes", 60, "free")
    status = await check_quota(db, uid, "dreaming_minutes", "free")
    assert status.used == 60
    assert status.exceeded is False

    await increment_usage(db, uid, "dreaming_minutes", 65, "free")
    status = await check_quota(db, uid, "dreaming_minutes", "free")
    assert status.used == 125
    assert status.exceeded is True


@pytest.mark.asyncio
async def test_dreaming_pre_flight_blocks_over_quota(db):
    """QueueService.check_task_quota blocks dreaming when minutes exceeded."""
    from p8.services.queue import QueueService

    uid = uuid4()
    # Seed usage past the free-plan dreaming_minutes limit (120)
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used) "
        "VALUES ($1, $2, date_trunc('month', CURRENT_DATE)::date, 125)",
        uid, "dreaming_minutes",
    )

    qs = QueueService(db)
    allowed = await qs.check_task_quota({
        "task_type": "dreaming",
        "user_id": uid,
    })
    assert allowed is False


@pytest.mark.asyncio
async def test_dreaming_pre_flight_allows_under_quota(db):
    """QueueService.check_task_quota allows dreaming when under quota."""
    from p8.services.queue import QueueService

    uid = uuid4()
    # Seed usage under the free-plan dreaming_minutes limit (120)
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used) "
        "VALUES ($1, $2, date_trunc('month', CURRENT_DATE)::date, 20)",
        uid, "dreaming_minutes",
    )

    qs = QueueService(db)
    allowed = await qs.check_task_quota({
        "task_type": "dreaming",
        "user_id": uid,
    })
    assert allowed is True


@pytest.mark.asyncio
async def test_dreaming_handler_increments_tokens(db):
    """DreamingHandler increments dreaming_io_tokens from agent run."""
    from unittest.mock import AsyncMock, patch
    from p8.workers.handlers.dreaming import DreamingHandler

    uid = uuid4()

    await db.execute(
        "DELETE FROM usage_tracking WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens'",
        uid,
    )

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.db = db
    ctx.encryption = None

    handler = DreamingHandler()

    agent_result = {"status": "ok", "io_tokens": 3000, "session_id": str(uuid4())}

    with patch.object(handler, "_run_dreaming_agent", new_callable=AsyncMock, return_value=agent_result):
        result = await handler.handle({"user_id": str(uid)}, ctx)

    assert result["io_tokens"] == 3000

    row = await db.fetchrow(
        "SELECT used FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens' "
        "AND period_start = date_trunc('month', CURRENT_DATE)::date",
        uid,
    )
    assert row is not None, "usage_tracking row should exist"
    assert row["used"] == 3000


@pytest.mark.asyncio
async def test_dreaming_handler_no_increment_on_skip(db):
    """No usage_tracking row when dreaming agent is skipped (zero tokens)."""
    from unittest.mock import AsyncMock, patch
    from p8.workers.handlers.dreaming import DreamingHandler

    uid = uuid4()

    await db.execute(
        "DELETE FROM usage_tracking WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens'",
        uid,
    )

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.db = db
    ctx.encryption = None

    handler = DreamingHandler()

    agent_result = {"status": "skipped_no_data", "io_tokens": 0}

    with patch.object(handler, "_run_dreaming_agent", new_callable=AsyncMock, return_value=agent_result):
        result = await handler.handle({"user_id": str(uid)}, ctx)

    assert result["io_tokens"] == 0

    row = await db.fetchrow(
        "SELECT used FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens' "
        "AND period_start = date_trunc('month', CURRENT_DATE)::date",
        uid,
    )
    assert row is None, "No usage_tracking row when tokens are 0"


@pytest.mark.llm
@pytest.mark.asyncio
async def test_dreaming_handler_increments_usage(db, encryption):
    """DreamingHandler records dreaming_io_tokens in usage_tracking after completion."""
    from p8.services.bootstrap import _export_api_keys
    from p8.settings import Settings
    from p8.api.tools import init_tools, set_tool_context
    from p8.workers.handlers.dreaming import DreamingHandler
    from tests.integration.dreaming.fixtures import TEST_USER_ID, setup_dreaming_fixtures

    _export_api_keys(Settings())
    init_tools(db, encryption)
    set_tool_context(user_id=TEST_USER_ID)

    # Clean prior dreaming state
    await db.execute(
        "DELETE FROM moments WHERE moment_type = 'dream' AND user_id = $1",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE mode = 'dreaming' AND user_id = $1)",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM sessions WHERE mode = 'dreaming' AND user_id = $1",
        TEST_USER_ID,
    )
    await db.execute(
        "DELETE FROM usage_tracking WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens'",
        TEST_USER_ID,
    )

    await setup_dreaming_fixtures(db, encryption)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.db = db
    ctx.encryption = encryption

    handler = DreamingHandler()
    result = await handler.handle(
        {"user_id": str(TEST_USER_ID), "lookback_days": 1},
        ctx,
    )

    phase2 = result.get("phase2", {})
    assert phase2.get("status") == "ok", f"Phase 2 failed: {phase2}"
    phase2_io = phase2.get("io_tokens", 0)
    assert phase2_io > 0, "Phase 2 should report non-zero io_tokens"

    # Verify usage_tracking row matches Phase 2's actual LLM token consumption
    row = await db.fetchrow(
        "SELECT used FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = 'dreaming_io_tokens' "
        "AND period_start = date_trunc('month', CURRENT_DATE)::date",
        TEST_USER_ID,
    )
    assert row is not None, "usage_tracking row for dreaming_io_tokens should exist"
    assert row["used"] == phase2_io, (
        f"Tracked usage ({row['used']}) should match Phase 2 io_tokens ({phase2_io})"
    )


# ── 11. Web searches daily quota ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_searches_daily_limits(db):
    """web_searches_daily: free=40, pro=100."""
    assert get_limits("free").web_searches_daily == 40
    assert get_limits("pro").web_searches_daily == 100
    assert get_limits("team").web_searches_daily == 200
    assert get_limits("enterprise").web_searches_daily == 1000


@pytest.mark.asyncio
async def test_web_searches_daily_quota(db):
    """web_searches_daily uses daily period and enforces limit."""
    uid = uuid4()

    # Fresh — no usage
    status = await check_quota(db, uid, "web_searches_daily", "free")
    assert status.limit == 40
    assert status.used == 0
    assert status.exceeded is False

    # Increment below limit
    await increment_usage(db, uid, "web_searches_daily", 20, "free")
    status = await check_quota(db, uid, "web_searches_daily", "free")
    assert status.used == 20
    assert status.exceeded is False

    # Increment past limit
    await increment_usage(db, uid, "web_searches_daily", 21, "free")
    status = await check_quota(db, uid, "web_searches_daily", "free")
    assert status.used == 41
    assert status.exceeded is True


@pytest.mark.asyncio
async def test_web_searches_daily_period_isolation(db):
    """Usage from yesterday does not affect today's web search quota."""
    uid = uuid4()
    # Seed usage for yesterday
    await db.execute(
        "INSERT INTO usage_tracking (user_id, resource_type, period_start, used) "
        "VALUES ($1, $2, CURRENT_DATE - 1, 10)",
        uid, "web_searches_daily",
    )
    # Today should show 0
    status = await check_quota(db, uid, "web_searches_daily", "free")
    assert status.used == 0
    assert status.exceeded is False
