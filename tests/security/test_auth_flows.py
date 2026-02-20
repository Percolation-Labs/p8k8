"""Tests for auth flows — JWT tokens, magic link, OAuth callbacks."""

from __future__ import annotations

import time

import jwt
import pytest
import pytest_asyncio

from p8.services.auth import AuthService
from p8.settings import Settings


@pytest_asyncio.fixture
async def auth(db, encryption):
    """AuthService with test settings."""
    settings = Settings()
    settings.auth_secret_key = "test-secret-key-for-jwt"
    settings.auth_access_token_expiry = 3600
    settings.auth_refresh_token_expiry = 2592000
    settings.auth_magic_link_expiry = 600
    return AuthService(db, encryption, settings)


@pytest_asyncio.fixture
async def test_user(auth, db, clean_db):
    """Create a test tenant + user for token tests."""
    # Clean stale users from prior runs (encrypted with different DEK)
    await db.execute("DELETE FROM users WHERE name = 'Test User'")
    tenant, user = await auth.create_personal_tenant("Test User", "test@example.com")
    return user, str(tenant.id)


# ---------------------------------------------------------------------------
# JWT token tests
# ---------------------------------------------------------------------------


class TestJWT:
    @pytest.mark.asyncio
    async def test_create_verify_access_token(self, auth, test_user):
        user, tenant_id = test_user
        token = auth.create_access_token(user, tenant_id)
        payload = auth.verify_token(token)

        assert payload["sub"] == str(user.id)
        assert payload["email"] == "test@example.com"
        assert payload["tenant_id"] == tenant_id
        assert payload["type"] == "access"

    @pytest.mark.asyncio
    async def test_expired_token_raises(self, auth, test_user):
        user, tenant_id = test_user
        # Manually create an already-expired token
        payload = {
            "sub": str(user.id),
            "email": user.email,
            "tenant_id": tenant_id,
            "type": "access",
            "exp": int(time.time()) - 10,
        }
        token = jwt.encode(payload, auth.settings.auth_secret_key, algorithm="HS256")

        with pytest.raises(jwt.ExpiredSignatureError):
            auth.verify_token(token)

    @pytest.mark.asyncio
    async def test_issue_and_refresh_tokens(self, auth, test_user):
        user, tenant_id = test_user
        tokens = await auth.issue_tokens(user, tenant_id)

        assert "access_token" in tokens
        assert "refresh_token" in tokens
        assert tokens["token_type"] == "bearer"
        assert tokens["expires_in"] == 3600

        # Verify access token works
        payload = auth.verify_token(tokens["access_token"])
        assert payload["sub"] == str(user.id)

    @pytest.mark.asyncio
    async def test_refresh_token_rotation(self, auth, test_user):
        user, tenant_id = test_user
        tokens = await auth.issue_tokens(user, tenant_id)

        # Refresh should return new tokens
        new_tokens = await auth.refresh_tokens(tokens["refresh_token"])
        assert new_tokens["access_token"] != tokens["access_token"]
        assert new_tokens["refresh_token"] != tokens["refresh_token"]

        # Old refresh token should now be consumed
        with pytest.raises(jwt.PyJWTError):
            await auth.refresh_tokens(tokens["refresh_token"])

    @pytest.mark.asyncio
    async def test_revoke_refresh_token(self, auth, test_user):
        user, tenant_id = test_user
        tokens = await auth.issue_tokens(user, tenant_id)

        # Manually extract jti and revoke
        payload = auth.verify_token(tokens["refresh_token"])
        await auth.revoke_refresh_jti(payload["jti"])

        # Refresh should fail after revocation
        with pytest.raises(jwt.PyJWTError):
            await auth.refresh_tokens(tokens["refresh_token"])


# ---------------------------------------------------------------------------
# Magic link tests
# ---------------------------------------------------------------------------


class TestMagicLink:
    @pytest.mark.asyncio
    async def test_magic_link_full_flow(self, auth, db, clean_db):
        """Create magic link, verify it, get user."""
        await db.execute("DELETE FROM users WHERE name = 'newuser@example.com'")
        token = await auth.create_magic_link_token("newuser@example.com")

        # Verify token and get user
        user, tenant_id = await auth.verify_magic_link(token)
        assert user.email == "newuser@example.com"
        assert tenant_id  # Should have created a tenant

    @pytest.mark.asyncio
    async def test_magic_link_single_use(self, auth, db, clean_db):
        """Magic link can only be used once."""
        await db.execute("DELETE FROM users WHERE name = 'single@example.com'")
        token = await auth.create_magic_link_token("single@example.com")

        # First use succeeds
        await auth.verify_magic_link(token)

        # Second use fails
        with pytest.raises(jwt.PyJWTError):
            await auth.verify_magic_link(token)

    @pytest.mark.asyncio
    async def test_magic_link_returning_user(self, auth, db, clean_db):
        """Returning user gets the same identity via magic link."""
        await db.execute("DELETE FROM users WHERE name = 'returning@example.com'")
        # First sign-in creates user
        token1 = await auth.create_magic_link_token("returning@example.com")
        user1, tenant1 = await auth.verify_magic_link(token1)

        # Second sign-in finds existing user
        token2 = await auth.create_magic_link_token("returning@example.com")
        user2, tenant2 = await auth.verify_magic_link(token2)

        assert str(user1.id) == str(user2.id)
        assert tenant1 == tenant2

    @pytest.mark.asyncio
    async def test_magic_link_expired(self, auth, clean_db):
        """Expired magic link is rejected."""
        # Manually create an expired token
        payload = {
            "email": "expired@example.com",
            "jti": "test-jti",
            "type": "magic_link",
            "exp": int(time.time()) - 10,
        }
        token = jwt.encode(payload, auth.settings.auth_secret_key, algorithm="HS256")

        with pytest.raises(jwt.ExpiredSignatureError):
            await auth.verify_magic_link(token)


# ---------------------------------------------------------------------------
# OAuth callback tests
# ---------------------------------------------------------------------------


class TestOAuthCallbacks:
    @pytest.mark.asyncio
    async def test_google_callback_creates_user(self, auth, db, clean_db):
        await db.execute("DELETE FROM users WHERE name = 'Alice Smith'")
        user_info = {
            "sub": "google-12345",
            "email": "alice@gmail.com",
            "name": "Alice Smith",
            "picture": "https://example.com/photo.jpg",
            "email_verified": True,
        }
        user, tenant_id = await auth.handle_google_callback(user_info)

        assert user.name == "Alice Smith"
        assert user.email == "alice@gmail.com"
        assert user.metadata["auth_provider"] == "google"
        assert user.metadata["provider_user_id"] == "google-12345"
        assert tenant_id

    @pytest.mark.asyncio
    async def test_google_callback_idempotent(self, auth, db, clean_db):
        """Repeated Google sign-in returns the same user."""
        await db.execute("DELETE FROM users WHERE name = 'Bob Jones'")
        user_info = {
            "sub": "google-99999",
            "email": "bob@gmail.com",
            "name": "Bob Jones",
        }
        user1, tenant1 = await auth.handle_google_callback(user_info)
        user2, tenant2 = await auth.handle_google_callback(user_info)

        assert str(user1.id) == str(user2.id)
        assert tenant1 == tenant2

    @pytest.mark.asyncio
    async def test_apple_callback_creates_user_with_name(self, auth, db, clean_db):
        await db.execute("DELETE FROM users WHERE name = 'Charlie Brown'")
        token_data = {"sub": "apple-00001", "email": "charlie@icloud.com"}
        user_info = {"firstName": "Charlie", "lastName": "Brown"}

        user, tenant_id = await auth.handle_apple_callback(token_data, user_info)

        assert user.name == "Charlie Brown"
        assert user.email == "charlie@icloud.com"
        assert user.metadata["auth_provider"] == "apple"
        assert user.metadata["provider_user_id"] == "apple-00001"

    @pytest.mark.asyncio
    async def test_apple_callback_without_name(self, auth, db, clean_db):
        """Apple only sends name on first auth — subsequent calls have no user_info."""
        await db.execute("DELETE FROM users WHERE name = 'Dave Wilson'")
        token_data = {"sub": "apple-00002", "email": "dave@icloud.com"}
        user_info = {"firstName": "Dave", "lastName": "Wilson"}

        # First auth — with name
        user1, _ = await auth.handle_apple_callback(token_data, user_info)
        assert user1.name == "Dave Wilson"

        # Second auth — no user_info
        user2, _ = await auth.handle_apple_callback(token_data, None)
        assert str(user1.id) == str(user2.id)
