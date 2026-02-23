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
  first day of the current month (or current day for daily resources like
  ``web_searches_daily``). Each row tracks ``used`` (accumulated counter)
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

GB = 1024 ** 3


MB = 1024 ** 2


@dataclass(frozen=True)
class PlanLimits:
    chat_tokens: int            # per month
    storage_bytes: int          # total
    dreaming_minutes: int       # per month
    cloud_folders: int
    dreaming_interval_hours: int
    max_file_size_bytes: int    # per-file upload limit
    worker_bytes_processed: int # monthly file processing budget
    dreaming_io_tokens: int     # monthly dreaming token budget
    web_searches_daily: int     # per day (Tavily API)
    news_searches_daily: int    # per day (platoon feed digest)


PLAN_LIMITS: dict[str, PlanLimits] = {
    "free":       PlanLimits(50_000,    20 * GB, 60,  2,   12, 20 * MB,  200 * MB,  20_000,     20, 2),
    "pro":        PlanLimits(100_000,   50 * GB, 120, 5,   12, 100 * MB,    1 * GB,  50_000,    50, 2),
    "team":       PlanLimits(100_000,  100 * GB, 180, 10,  12, 500 * MB,    5 * GB, 100_000,   100, 5),
    "enterprise": PlanLimits(250_000,  500 * GB, 360, 999,  6,   1 * GB,   50 * GB, 500_000,   500, 10),
}

# Resources tracked with daily periods instead of monthly.
_DAILY_RESOURCES = {"web_searches_daily", "news_searches_daily"}


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
    trunc = "day" if resource_type in _DAILY_RESOURCES else "month"
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

    trunc = "day" if resource_type in _DAILY_RESOURCES else "month"
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
