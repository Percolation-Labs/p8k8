"""Integration test for MCP OAuth 2.1 token exchange.

Sets up real DB state (user with devices, OAuth client, auth code) and
verifies the full exchange_authorization_code flow end-to-end — including
the devices JSON string parsing that caused production failures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

import pytest
import pytest_asyncio

from p8.services.auth import AuthService
from p8.settings import Settings


@pytest_asyncio.fixture
async def auth(db, encryption):
    settings = Settings()
    settings.auth_secret_key = "test-secret-key-for-jwt"
    settings.auth_access_token_expiry = 3600
    settings.auth_refresh_token_expiry = 2592000
    return AuthService(db, encryption, settings)


@pytest_asyncio.fixture
async def test_user_with_devices(auth, db, clean_db):
    """Create a user with a devices list (stored as JSON string in DB)."""
    await db.execute("DELETE FROM users WHERE name = 'MCP Test User'")
    tenant, user = await auth.create_personal_tenant("MCP Test User", "mcp-test@example.com")

    # Add devices — stored as JSON string in the DB column, which is the
    # root cause of the production bug (Pydantic expects list, gets string)
    devices = [{"platform": "fcm", "token": "test-token-123", "active": True}]
    await db.execute(
        "UPDATE users SET devices = $1 WHERE id = $2",
        json.dumps(devices), user.id,
    )
    return user, str(tenant.id)


def _make_pkce():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TestMcpTokenExchange:
    """Full integration test for the MCP OAuth token exchange."""

    @pytest.mark.asyncio
    async def test_full_exchange_with_devices(self, auth, test_user_with_devices):
        """Token exchange must succeed even when user.devices is a JSON string in DB."""
        user, tenant_id = test_user_with_devices
        verifier, challenge = _make_pkce()

        # 1. Register OAuth client
        client = await auth.register_client({
            "redirect_uris": ["https://example.com/callback"],
            "client_name": "integration-test",
        })

        # 2. Create authorization code with PKCE
        code = await auth.create_authorization_code(
            client_id=client["client_id"],
            redirect_uri="https://example.com/callback",
            code_challenge=challenge,
            scope="openid",
            provider="google",
            provider_state="test-state",
        )

        # 3. Attach user (simulates what callback does after Google auth)
        await auth.set_authorization_code_user(
            code, str(user.id), tenant_id, email="mcp-test@example.com",
        )

        # 4. Exchange code for tokens (this is where the devices bug hit)
        tokens = await auth.exchange_authorization_code(
            code=code,
            client_id=client["client_id"],
            code_verifier=verifier,
            redirect_uri="https://example.com/callback",
        )

        assert "access_token" in tokens
        assert tokens["token_type"] == "bearer"
        assert "refresh_token" in tokens

        # Verify the token is valid
        payload = auth.verify_token(tokens["access_token"])
        assert payload["sub"] == str(user.id)
        assert payload["tenant_id"] == tenant_id

    @pytest.mark.asyncio
    async def test_exchange_without_devices(self, auth, db, clean_db):
        """Token exchange works for user without devices."""
        await db.execute("DELETE FROM users WHERE name = 'MCP No Device'")
        tenant, user = await auth.create_personal_tenant("MCP No Device", "nodev@example.com")
        verifier, challenge = _make_pkce()

        client = await auth.register_client({
            "redirect_uris": ["https://example.com/callback"],
        })
        code = await auth.create_authorization_code(
            client_id=client["client_id"],
            redirect_uri="https://example.com/callback",
            code_challenge=challenge,
            scope="openid",
            provider="google",
        )
        await auth.set_authorization_code_user(code, str(user.id), str(tenant.id))

        tokens = await auth.exchange_authorization_code(
            code=code,
            client_id=client["client_id"],
            code_verifier=verifier,
            redirect_uri="https://example.com/callback",
        )
        assert "access_token" in tokens
        assert tokens["token_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_exchange_bad_verifier_fails(self, auth, db, clean_db):
        """Wrong code_verifier must fail PKCE check."""
        await db.execute("DELETE FROM users WHERE name = 'MCP PKCE'")
        tenant, user = await auth.create_personal_tenant("MCP PKCE", "pkce@example.com")
        _, challenge = _make_pkce()

        client = await auth.register_client({
            "redirect_uris": ["https://example.com/callback"],
        })
        code = await auth.create_authorization_code(
            client_id=client["client_id"],
            redirect_uri="https://example.com/callback",
            code_challenge=challenge,
            scope="openid",
            provider="google",
        )
        await auth.set_authorization_code_user(code, str(user.id), str(tenant.id))

        with pytest.raises(ValueError, match="PKCE"):
            await auth.exchange_authorization_code(
                code=code,
                client_id=client["client_id"],
                code_verifier="wrong-verifier",
                redirect_uri="https://example.com/callback",
            )

    @pytest.mark.asyncio
    async def test_code_single_use(self, auth, db, clean_db):
        """Auth code must be consumed (deleted) after first exchange."""
        await db.execute("DELETE FROM users WHERE name = 'MCP Single'")
        tenant, user = await auth.create_personal_tenant("MCP Single", "single@example.com")
        verifier, challenge = _make_pkce()

        client = await auth.register_client({
            "redirect_uris": ["https://example.com/callback"],
        })
        code = await auth.create_authorization_code(
            client_id=client["client_id"],
            redirect_uri="https://example.com/callback",
            code_challenge=challenge,
            scope="openid",
            provider="google",
        )
        await auth.set_authorization_code_user(code, str(user.id), str(tenant.id))

        # First exchange succeeds
        tokens = await auth.exchange_authorization_code(
            code=code,
            client_id=client["client_id"],
            code_verifier=verifier,
            redirect_uri="https://example.com/callback",
        )
        assert "access_token" in tokens

        # Second exchange fails (code consumed)
        with pytest.raises(ValueError, match="Invalid or expired"):
            await auth.exchange_authorization_code(
                code=code,
                client_id=client["client_id"],
                code_verifier=verifier,
                redirect_uri="https://example.com/callback",
            )
