"""Unit tests for SecurityContext and resolve_security_context."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from p8.api.security import PermissionLevel, SecurityContext, resolve_security_context


# ---------------------------------------------------------------------------
# SecurityContext — effective_* properties
# ---------------------------------------------------------------------------


class TestEffectiveProperties:
    def test_user_level_returns_user_id(self):
        uid = uuid4()
        ctx = SecurityContext(level=PermissionLevel.USER, user_id=uid, tenant_id="t1")
        assert ctx.effective_user_id == uid
        assert ctx.effective_tenant_id == "t1"

    def test_tenant_level_returns_no_user_id(self):
        ctx = SecurityContext(level=PermissionLevel.TENANT, tenant_id="t1")
        assert ctx.effective_user_id is None
        assert ctx.effective_tenant_id == "t1"

    def test_master_level_returns_nothing(self):
        ctx = SecurityContext.master()
        assert ctx.effective_user_id is None
        assert ctx.effective_tenant_id is None

    def test_system_is_master(self):
        ctx = SecurityContext.system()
        assert ctx.level == PermissionLevel.MASTER


# ---------------------------------------------------------------------------
# SecurityContext — can_access_record
# ---------------------------------------------------------------------------


class TestCanAccessRecord:
    def test_master_sees_everything(self):
        ctx = SecurityContext.master()
        uid = uuid4()
        assert ctx.can_access_record(uid, "t1") is True
        assert ctx.can_access_record(None, None) is True

    def test_tenant_sees_own_tenant(self):
        ctx = SecurityContext(level=PermissionLevel.TENANT, tenant_id="t1")
        assert ctx.can_access_record(uuid4(), "t1") is True

    def test_tenant_denied_other_tenant(self):
        ctx = SecurityContext(level=PermissionLevel.TENANT, tenant_id="t1")
        assert ctx.can_access_record(uuid4(), "t2") is False

    def test_tenant_sees_unscoped_records(self):
        ctx = SecurityContext(level=PermissionLevel.TENANT, tenant_id="t1")
        assert ctx.can_access_record(uuid4(), None) is True

    def test_user_sees_own_records(self):
        uid = uuid4()
        ctx = SecurityContext(level=PermissionLevel.USER, user_id=uid)
        assert ctx.can_access_record(uid, "t1") is True

    def test_user_denied_other_user(self):
        ctx = SecurityContext(level=PermissionLevel.USER, user_id=uuid4())
        assert ctx.can_access_record(uuid4(), "t1") is False

    def test_user_sees_shared_records(self):
        ctx = SecurityContext(level=PermissionLevel.USER, user_id=uuid4())
        assert ctx.can_access_record(None, "t1") is True

    def test_user_accepts_string_uuid(self):
        uid = uuid4()
        ctx = SecurityContext(level=PermissionLevel.USER, user_id=uid)
        assert ctx.can_access_record(str(uid), "t1") is True


# ---------------------------------------------------------------------------
# resolve_security_context
# ---------------------------------------------------------------------------


def _make_request(
    *,
    headers: dict[str, str] | None = None,
    api_key: str = "",
    master_key: str = "",
    tenant_keys: str = "",
    auth_secret_key: str = "test-secret",
    auth_svc: MagicMock | None = None,
) -> MagicMock:
    """Build a mock FastAPI Request with the given headers and app settings."""
    request = MagicMock()
    request.headers = headers or {}
    settings = MagicMock()
    settings.api_key = api_key
    settings.master_key = master_key
    settings.tenant_keys = tenant_keys
    settings.auth_secret_key = auth_secret_key
    request.app.state.settings = settings
    if auth_svc:
        request.app.state.auth = auth_svc
    return request


@pytest.mark.asyncio
class TestResolveSecurityContext:
    async def test_master_key_bearer(self):
        request = _make_request(
            headers={"authorization": "Bearer master-secret"},
            master_key="master-secret",
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.MASTER

    async def test_master_key_x_api_key(self):
        request = _make_request(
            headers={"x-api-key": "master-secret"},
            master_key="master-secret",
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.MASTER

    async def test_tenant_key(self):
        import json
        request = _make_request(
            headers={"authorization": "Bearer tenant-abc-key"},
            tenant_keys=json.dumps({"tenant-abc": "tenant-abc-key"}),
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.TENANT
        assert ctx.tenant_id == "tenant-abc"

    async def test_jwt_user(self):
        uid = uuid4()
        auth_svc = MagicMock()
        auth_svc.verify_token.return_value = {
            "type": "access",
            "sub": str(uid),
            "tenant_id": "t1",
            "email": "user@example.com",
            "provider": "google",
        }
        request = _make_request(
            headers={"authorization": "Bearer valid-jwt"},
            auth_svc=auth_svc,
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.USER
        assert ctx.user_id == uid
        assert ctx.tenant_id == "t1"

    async def test_x_user_id_header(self):
        uid = uuid4()
        request = _make_request(
            headers={"x-user-id": str(uid), "x-tenant-id": "t2"},
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.USER
        assert ctx.user_id == uid

    async def test_legacy_api_key(self):
        request = _make_request(
            headers={"authorization": "Bearer legacy-key"},
            api_key="legacy-key",
        )
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.MASTER

    async def test_no_auth_configured_open_dev(self):
        request = _make_request()
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            ctx = await resolve_security_context(request)
        assert ctx.level == PermissionLevel.MASTER

    async def test_auth_configured_no_creds_401(self):
        from fastapi import HTTPException
        request = _make_request(api_key="required-key")
        with patch("p8.api.security.get_settings", return_value=request.app.state.settings):
            with pytest.raises(HTTPException) as exc_info:
                await resolve_security_context(request)
            assert exc_info.value.status_code == 401
