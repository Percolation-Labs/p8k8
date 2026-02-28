"""Usage tracking — plan limits, quota checks, increment.

Quotas enforce per-user resource limits based on their Stripe plan (free/pro/
team/enterprise). Each plan defines caps for chat tokens, storage, dreaming
minutes, cloud folders, file size, worker processing, dreaming I/O tokens,
and web searches.

Storage
-------
- **Where stored:** The ``files`` table (``size_bytes`` column). There is no
  separate counter — storage usage is computed on-the-fly as
  ``SUM(size_bytes)`` over non-deleted rows for the user.
- **Checked:** Before every file upload in ``POST /content/`` (pre-flight).
  If ``current_used + file_size > limit``, the request is rejected with 429.
- **Updated:** Implicitly when files are inserted or soft-deleted.

Periodic resources (chat_tokens, dreaming_minutes, worker_bytes_processed, …)
------------------------------------------------------------------------------
- **Where stored:** The ``usage_tracking`` table, partitioned by
  ``(user_id, resource_type, period_start)`` where ``period_start`` is the
  start of the current week (Monday, via ``date_trunc('week', …)``) for
  weekly resources, or the current day for daily resources like
  ``web_searches_daily``. Each row tracks ``used`` (accumulated counter)
  and ``granted_extra`` (add-on credits that extend the base limit).
- **Checked:** Before the action that consumes the resource. For chat tokens
  this is a pre-flight check in ``POST /chat/{chat_id}`` — if
  ``used > limit + granted_extra`` the request is rejected with 429.
- **Updated:** After the action completes. For chat tokens,
  ``increment_usage()`` is called post-flight with a token estimate. The
  underlying ``usage_increment()`` SQL function performs an atomic
  INSERT … ON CONFLICT upsert to avoid races.

Plan resolution
---------------
A user's plan is looked up from ``stripe_customers`` (by ``user_id`` +
``tenant_id``). The result is cached in-memory for 5 minutes
(``_plan_cache``). Users with no ``stripe_customers`` row default to "free".
Unknown plan IDs also fall back to the free tier limits.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from p8.services.database import Database

logger = logging.getLogger(__name__)

TB = 1024 ** 4

GB = 1024 ** 3

MB = 1024 ** 2


@dataclass(frozen=True)
class PlanLimits:
    chat_tokens: int            # per day
    storage_bytes: int          # total
    dreaming_minutes: int       # per week
    cloud_folders: int
    dreaming_interval_hours: int
    max_file_size_bytes: int    # per-file upload limit
    worker_bytes_processed: int # weekly file processing budget
    dreaming_io_tokens: int     # per day
    web_searches_daily: int     # per day (Tavily API)
    news_searches_daily: int    # per day (platoon feed digest)


PLAN_LIMITS: dict[str, PlanLimits] = {
    "free":       PlanLimits( 50_000,    40 * GB,  30,  4,  12,  40 * MB, 100 * MB,  20_000,     40, 4),
    "pro":        PlanLimits(100_000,   100 * GB,  60, 10,  12, 200 * MB, 500 * MB,  40_000,    100, 4),
    "team":       PlanLimits(100_000,   200 * GB,  90, 20,  12,   1 * GB,   2 * GB,  40_000,    200, 10),
    "enterprise": PlanLimits(200_000,     1 * TB, 180, 999,  6,   2 * GB,  25 * GB,  80_000,   1000, 20),
}

# Resources tracked with daily periods.
_DAILY_RESOURCES = {"chat_tokens", "dreaming_io_tokens", "web_searches_daily", "news_searches_daily"}


@dataclass
class QuotaStatus:
    used: int
    limit: int
    exceeded: bool


# ── Cached plan lookup ────────────────────────────────────────────────────

_plan_cache: dict[tuple[UUID, str | None], tuple[str, float]] = {}
_PLAN_CACHE_TTL = 300  # 5 minutes


async def get_user_plan(db: Database, user_id: UUID, tenant_id: str | None = None) -> str:
    """Return the plan_id for a user, cached in-memory for 5 minutes."""
    key = (user_id, tenant_id)
    now = time.monotonic()

    cached = _plan_cache.get(key)
    if cached and (now - cached[1]) < _PLAN_CACHE_TTL:
        return cached[0]

    row = await db.fetchrow(
        "SELECT plan_id FROM stripe_customers "
        "WHERE user_id = $1 AND tenant_id IS NOT DISTINCT FROM $2 "
        "AND deleted_at IS NULL",
        user_id, tenant_id,
    )
    plan_id = row["plan_id"] if row else "free"
    _plan_cache[key] = (plan_id, now)
    return plan_id


def get_limits(plan_id: str) -> PlanLimits:
    """Return PlanLimits for a plan_id, defaulting to free."""
    return PLAN_LIMITS.get(plan_id, PLAN_LIMITS["free"])


# ── Quota checking ────────────────────────────────────────────────────────

async def check_quota(
    db: Database,
    user_id: UUID,
    resource_type: str,
    plan_id: str,
) -> QuotaStatus:
    """Check current usage against plan limits without incrementing.

    For storage_bytes, computes on-the-fly from files table.
    For periodic resources, reads usage_tracking.
    """
    limits = get_limits(plan_id)

    if resource_type == "storage_bytes":
        used = int(await db.fetchval(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM files "
            "WHERE user_id = $1 AND deleted_at IS NULL",
            user_id,
        ))
        limit = limits.storage_bytes
        return QuotaStatus(used=used, limit=limit, exceeded=used > limit)

    # Periodic resource — check usage_tracking
    limit_value = getattr(limits, resource_type, 0)
    trunc = "day" if resource_type in _DAILY_RESOURCES else "week"
    row = await db.fetchrow(
        "SELECT used, granted_extra FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = $2 "
        f"AND period_start = date_trunc('{trunc}', CURRENT_DATE)::date",
        user_id, resource_type,
    )
    used = row["used"] if row else 0
    extra = row["granted_extra"] if row else 0
    effective_limit = limit_value + extra
    return QuotaStatus(used=used, limit=effective_limit, exceeded=used > effective_limit)


async def increment_usage(
    db: Database,
    user_id: UUID,
    resource_type: str,
    amount: int,
    plan_id: str,
) -> QuotaStatus:
    """Atomically increment usage and return updated status.

    Uses the usage_increment() SQL function for race-free upsert.
    """
    limits = get_limits(plan_id)
    limit_value = getattr(limits, resource_type, 0)

    trunc = "day" if resource_type in _DAILY_RESOURCES else "week"
    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4, "
        f"date_trunc('{trunc}', CURRENT_DATE)::date)",
        user_id, resource_type, amount, limit_value,
    )
    return QuotaStatus(
        used=row["new_used"],
        limit=row["effective_limit"],
        exceeded=row["exceeded"],
    )


async def get_all_usage(
    db: Database,
    user_id: UUID,
    plan_id: str,
) -> dict:
    """Return usage summary for all metered resources (for /billing/usage)."""
    limits = get_limits(plan_id)

    # Chat tokens
    chat = await check_quota(db, user_id, "chat_tokens", plan_id)

    # Dreaming minutes
    dreaming = await check_quota(db, user_id, "dreaming_minutes", plan_id)

    # Storage (computed from files table)
    storage = await check_quota(db, user_id, "storage_bytes", plan_id)

    # Web searches (daily)
    web = await check_quota(db, user_id, "web_searches_daily", plan_id)

    return {
        "plan_id": plan_id,
        "chat_tokens": {"used": chat.used, "limit": chat.limit, "exceeded": chat.exceeded},
        "dreaming_minutes": {"used": dreaming.used, "limit": dreaming.limit, "exceeded": dreaming.exceeded},
        "storage_bytes": {"used": storage.used, "limit": storage.limit, "exceeded": storage.exceeded},
        "web_searches_daily": {"used": web.used, "limit": web.limit, "exceeded": web.exceeded},
        "dreaming_interval_hours": limits.dreaming_interval_hours,
        "cloud_folders": limits.cloud_folders,
    }


# ── Multi-tenant usage overview (for reports) ───────────────────────────

# Resource columns shown in the report pivot table.
REPORT_COLUMNS: list[tuple[str, str, str]] = [
    # (resource_type, short_label, period)
    ("chat_tokens", "Chat Tokens", "day"),
    ("dreaming_io_tokens", "Dream IO", "day"),
    ("web_searches_daily", "Web Search", "day"),
    ("news_searches_daily", "News", "day"),
    ("worker_bytes_processed", "Files", "wk"),
    ("dreaming_minutes", "Dream Min", "wk"),
]


async def get_tenant_plans(db: Database) -> dict[str, str]:
    """Return {tenant_id: plan_id} for all active subscriptions."""
    rows = await db.fetch(
        "SELECT tenant_id, plan_id FROM stripe_customers "
        "WHERE deleted_at IS NULL AND tenant_id IS NOT NULL"
    )
    return {r["tenant_id"]: r["plan_id"] for r in rows}


async def get_all_tenant_ids(db: Database) -> list[str]:
    """Return all active tenant_ids from the users table."""
    rows = await db.fetch(
        "SELECT DISTINCT tenant_id FROM users "
        "WHERE tenant_id IS NOT NULL AND deleted_at IS NULL"
    )
    return [r["tenant_id"] for r in rows]


async def get_usage_by_tenant(db: Database) -> dict[str, dict[str, int]]:
    """Return {tenant_id: {resource_type: used}} for the current period.

    Includes ALL active tenants (even those with zero usage).
    Uses the latest period_start per (user, resource) within the current month
    to handle the monthly→weekly transition gracefully.
    """
    # Get all active tenants so everyone appears in the report
    all_tenants = await get_all_tenant_ids(db)

    rows = await db.fetch(
        "SELECT DISTINCT ON (ut.user_id, ut.resource_type) "
        "       u.tenant_id, ut.resource_type, ut.used "
        "  FROM usage_tracking ut "
        "  LEFT JOIN users u ON ut.user_id = u.id "
        " WHERE ut.period_start >= date_trunc('month', CURRENT_DATE)::date "
        " ORDER BY ut.user_id, ut.resource_type, ut.period_start DESC"
    )
    from collections import defaultdict
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Seed all tenants so they appear even with zero usage
    for tid in all_tenants:
        result[tid]  # triggers defaultdict creation
    for r in rows:
        tid = r["tenant_id"] or ""
        result[tid][r["resource_type"]] += r["used"]
    return dict(result)
