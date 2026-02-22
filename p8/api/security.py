"""Security context — three-tier permission model: USER → TENANT → MASTER.

Every request is resolved to a SecurityContext that carries the caller's
permission level, user_id, and tenant_id.  Repository and REM queries use
the effective_* properties to scope database access automatically.
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from fastapi import HTTPException, Request

from p8.settings import get_settings

logger = logging.getLogger(__name__)


class PermissionLevel(str, Enum):
    USER = "user"
    TENANT = "tenant"
    MASTER = "master"


@dataclass(frozen=True)
class SecurityContext:
    level: PermissionLevel
    user_id: UUID | None = None
    tenant_id: str | None = None
    email: str = ""
    provider: str = ""
    scopes: list[str] = field(default_factory=list)

    # -- effective accessors used by Repository / REM queries ---------------

    @property
    def effective_user_id(self) -> UUID | None:
        """Return user_id only at USER level (filters to own records)."""
        return self.user_id if self.level == PermissionLevel.USER else None

    @property
    def effective_tenant_id(self) -> str | None:
        """Return tenant_id at USER/TENANT level, None at MASTER."""
        if self.level in (PermissionLevel.USER, PermissionLevel.TENANT):
            return self.tenant_id
        return None

    def can_access_record(
        self,
        record_user_id: UUID | str | None,
        record_tenant_id: str | None,
    ) -> bool:
        """Post-fetch check: can this context see the given record?"""
        if self.level == PermissionLevel.MASTER:
            return True

        # Tenant level — record must belong to the same tenant (or be unscoped)
        if self.level == PermissionLevel.TENANT:
            if record_tenant_id and self.tenant_id:
                return record_tenant_id == self.tenant_id
            return True  # unscoped records are visible

        # User level — own records + shared (user_id IS NULL)
        if record_user_id is not None:
            rec_uid = UUID(str(record_user_id)) if not isinstance(record_user_id, UUID) else record_user_id
            return rec_uid == self.user_id
        return True  # shared records (user_id IS NULL) are visible

    # -- convenience constructors -------------------------------------------

    @classmethod
    def master(cls) -> SecurityContext:
        """Internal callers (CLI, workers, migrations)."""
        return cls(level=PermissionLevel.MASTER)

    @classmethod
    def system(cls) -> SecurityContext:
        """Alias for master — used by background workers."""
        return cls.master()


async def resolve_security_context(request: Request) -> SecurityContext:
    """FastAPI dependency that resolves a SecurityContext from the request.

    Resolution order:
    1. Master key match → MASTER
    2. Tenant key match → TENANT
    3. JWT Bearer token → USER
    4. x-user-id header → USER (dev mode)
    5. Legacy api_key match → MASTER (backward compat)
    6. No auth configured → MASTER (open dev)
    7. Else → 401
    """
    settings = get_settings()

    auth_header = request.headers.get("authorization", "")
    bearer_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    x_api_key = request.headers.get("x-api-key", "")

    # 1. Master key
    if settings.master_key:
        for candidate in (bearer_token, x_api_key):
            if candidate and hmac.compare_digest(candidate, settings.master_key):
                return SecurityContext(level=PermissionLevel.MASTER)

    # 2. Tenant keys (JSON: {"tenant_id": "key", ...})
    if settings.tenant_keys:
        try:
            tenant_map: dict[str, str] = json.loads(settings.tenant_keys)
        except (json.JSONDecodeError, TypeError):
            tenant_map = {}
        for candidate in (bearer_token, x_api_key):
            if candidate:
                for tid, tkey in tenant_map.items():
                    if hmac.compare_digest(candidate, tkey):
                        return SecurityContext(
                            level=PermissionLevel.TENANT, tenant_id=tid
                        )

    # 3. JWT Bearer token → USER
    if bearer_token:
        # Skip if it matched api_key (handled below as legacy)
        is_legacy = settings.api_key and hmac.compare_digest(bearer_token, settings.api_key)
        if not is_legacy:
            try:
                auth_svc = request.app.state.auth
                payload = auth_svc.verify_token(bearer_token)
                if payload.get("type") == "access":
                    return SecurityContext(
                        level=PermissionLevel.USER,
                        user_id=UUID(payload["sub"]),
                        tenant_id=payload.get("tenant_id", ""),
                        email=payload.get("email", ""),
                        provider=payload.get("provider", ""),
                        scopes=payload.get("scopes", []),
                    )
            except Exception:
                pass  # fall through to other checks

    # 4. x-user-id header (dev mode)
    raw_user_id = request.headers.get("x-user-id")
    if raw_user_id:
        try:
            return SecurityContext(
                level=PermissionLevel.USER,
                user_id=UUID(raw_user_id),
                tenant_id=request.headers.get("x-tenant-id", ""),
                email=request.headers.get("x-user-email", ""),
                provider="header",
            )
        except ValueError:
            pass

    # 5. Legacy api_key match → MASTER (backward compat)
    if settings.api_key:
        for candidate in (bearer_token, x_api_key):
            if candidate and hmac.compare_digest(candidate, settings.api_key):
                return SecurityContext(level=PermissionLevel.MASTER)

    # 6. No auth configured at all → MASTER (open dev)
    if not settings.api_key and not settings.master_key:
        return SecurityContext(level=PermissionLevel.MASTER)

    # 7. Else → 401
    raise HTTPException(401, "Missing or invalid authentication")
