"""Unit tests for OAuth 2.1 authorization code methods in AuthService.

Reproduces the JSONB double-encoding bug: when asyncpg's JSONB codec
(set_type_codec with json.dumps encoder) is active and the SQL uses
$1::jsonb, asyncpg double-encodes the string parameter. PostgreSQL then
sees a JSONB scalar string instead of an object, and the || operator
produces an array instead of a merged object.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from tests.unit.helpers import mock_services


def _make_auth_service():
    """Create an AuthService with mocked DB."""
    db, encryption, settings, *_ = mock_services()
    settings.auth_secret_key = "test-secret-key-for-jwt-signing"
    settings.google_client_id = "test-google-id"
    settings.google_client_secret = "test-google-secret"
    db.fetchrow = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value="INSERT 0 1")

    from p8.services.auth import AuthService
    svc = AuthService(db, encryption, settings)
    return svc, db


@pytest.fixture
def auth_svc():
    return _make_auth_service()


AUTH_CODE_RECORD = {
    "client_id": "test-client-123",
    "redirect_uri": "https://example.com/callback",
    "code_challenge": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "scope": "openid",
    "provider": "google",
    "client_state": "xyzstate",
}


class TestGetAuthorizationCode:
    """Test get_authorization_code parsing of content_summary."""

    @pytest.mark.anyio
    async def test_normal_json_string(self, auth_svc):
        """Happy path: content_summary is a JSON object string."""
        svc, db = auth_svc
        db.fetchrow = AsyncMock(return_value={
            "content_summary": json.dumps(AUTH_CODE_RECORD),
        })

        result = await svc.get_authorization_code("test-code")
        assert result == AUTH_CODE_RECORD
        assert result["client_id"] == "test-client-123"

    @pytest.mark.anyio
    async def test_not_found(self, auth_svc):
        """Returns None when code doesn't exist."""
        svc, db = auth_svc
        db.fetchrow = AsyncMock(return_value=None)

        result = await svc.get_authorization_code("nonexistent")
        assert result is None

    @pytest.mark.anyio
    async def test_jsonb_double_encoded_array(self, auth_svc):
        """Bug repro: JSONB || scalar produces an array instead of merged object.

        When asyncpg double-encodes the patch parameter, PostgreSQL's
        jsonb || operator concatenates object + scalar as an array:
          [{"client_id":...}, "{\"user_id\":...}"]

        The parser must recover the merged dict from this array.
        """
        svc, db = auth_svc
        # Simulate the corrupted content_summary after double-encoding
        user_patch = {"user_id": "uid-123", "tenant_id": "tid-456", "email": "test@example.com"}
        corrupted = [AUTH_CODE_RECORD, json.dumps(user_patch)]
        db.fetchrow = AsyncMock(return_value={
            "content_summary": json.dumps(corrupted),
        })

        result = await svc.get_authorization_code("test-code")
        assert result is not None
        # Should contain both original fields and user fields
        assert result["client_id"] == "test-client-123"
        assert result["user_id"] == "uid-123"
        assert result["tenant_id"] == "tid-456"


class TestConsumeAuthorizationCode:
    """Test consume_authorization_code parsing."""

    @pytest.mark.anyio
    async def test_normal_json_string(self, auth_svc):
        """Happy path: consumes and returns the record."""
        svc, db = auth_svc
        merged = {**AUTH_CODE_RECORD, "user_id": "uid-123", "tenant_id": "tid-456"}
        db.fetchrow = AsyncMock(return_value={
            "content_summary": json.dumps(merged),
        })

        result = await svc.consume_authorization_code("test-code")
        assert result == merged

    @pytest.mark.anyio
    async def test_jsonb_double_encoded_array(self, auth_svc):
        """Consume must also handle the array corruption."""
        svc, db = auth_svc
        user_patch = {"user_id": "uid-123", "tenant_id": "tid-456", "email": "test@example.com"}
        corrupted = [AUTH_CODE_RECORD, json.dumps(user_patch)]
        db.fetchrow = AsyncMock(return_value={
            "content_summary": json.dumps(corrupted),
        })

        result = await svc.consume_authorization_code("test-code")
        assert result is not None
        assert result["client_id"] == "test-client-123"
        assert result["user_id"] == "uid-123"


class TestSetAuthorizationCodeUser:
    """Test that set_authorization_code_user avoids JSONB double-encoding."""

    @pytest.mark.anyio
    async def test_writes_merged_json_text(self, auth_svc):
        """Should read-merge-write as plain TEXT, not use JSONB casting."""
        svc, db = auth_svc
        db.fetchrow = AsyncMock(return_value={
            "content_summary": json.dumps(AUTH_CODE_RECORD),
        })
        db.execute = AsyncMock(return_value="UPDATE 1")

        await svc.set_authorization_code_user(
            "test-code", "uid-123", "tid-456", email="test@example.com",
        )

        # Verify the UPDATE writes a plain JSON string (no ::jsonb cast)
        db.execute.assert_called_once()
        call_args = db.execute.call_args
        sql = call_args[0][0]
        written_json = call_args[0][1]

        assert "::jsonb" not in sql, "Should not use ::jsonb cast to avoid double-encoding"
        parsed = json.loads(written_json)
        assert parsed["client_id"] == "test-client-123"
        assert parsed["user_id"] == "uid-123"
        assert parsed["tenant_id"] == "tid-456"
        assert parsed["email"] == "test@example.com"
