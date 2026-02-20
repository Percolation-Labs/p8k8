"""Usage tracking — plan limits, quota checks, increment."""

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


PLAN_LIMITS: dict[str, PlanLimits] = {
    "free":       PlanLimits(25_000,    10 * GB, 30,  1,   24, 10 * MB,  100 * MB,  10_000),
    "pro":        PlanLimits(100_000,   50 * GB, 120, 5,   12, 100 * MB,    1 * GB,  50_000),
    "team":       PlanLimits(100_000,  100 * GB, 180, 10,  12, 500 * MB,    5 * GB, 100_000),
    "enterprise": PlanLimits(250_000,  500 * GB, 360, 999,  6,   1 * GB,   50 * GB, 500_000),
}


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
        used = await db.fetchval(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM files "
            "WHERE user_id = $1 AND deleted_at IS NULL",
            user_id,
        )
        limit = limits.storage_bytes
        return QuotaStatus(used=used, limit=limit, exceeded=used > limit)

    # Periodic resource — check usage_tracking
    limit_value = getattr(limits, resource_type, 0)
    row = await db.fetchrow(
        "SELECT used, granted_extra FROM usage_tracking "
        "WHERE user_id = $1 AND resource_type = $2 "
        "AND period_start = date_trunc('month', CURRENT_DATE)::date",
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

    row = await db.fetchrow(
        "SELECT * FROM usage_increment($1, $2, $3, $4)",
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

    return {
        "plan_id": plan_id,
        "chat_tokens": {"used": chat.used, "limit": chat.limit, "exceeded": chat.exceeded},
        "dreaming_minutes": {"used": dreaming.used, "limit": dreaming.limit, "exceeded": dreaming.exceeded},
        "storage_bytes": {"used": storage.used, "limit": storage.limit, "exceeded": storage.exceeded},
        "dreaming_interval_hours": limits.dreaming_interval_hours,
        "cloud_folders": limits.cloud_folders,
    }
