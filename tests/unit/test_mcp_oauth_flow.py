"""End-to-end unit test for the MCP OAuth 2.1 flow.

Simulates: register → authorize → Google callback → token exchange
Uses a real FastAPI TestClient with mocked AuthService and OAuth provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from p8.ontology.types import User


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CLIENT_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
GOOGLE_USER_INFO = {
    "sub": "google-user-123",
    "email": "test@example.com",
    "name": "Test User",
    "email_verified": True,
}


def _make_pkce():
    """Generate PKCE code_verifier + code_challenge (S256)."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _make_mock_auth():
    """Create a mock AuthService with working OAuth 2.1 methods.

    Uses a dict-backed in-memory store for clients and auth codes,
    so the full flow can be tested end-to-end without a database.
    """
    auth = MagicMock()
    clients: dict[str, dict] = {}
    auth_codes: dict[str, dict] = {}

    # --- register_client ---
    async def register_client(metadata):
        client_id = str(uuid4())
        client_secret = str(uuid4())
        record = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": metadata.get("redirect_uris", []),
            "client_name": metadata.get("client_name", ""),
        }
        clients[client_id] = record
        return record

    auth.register_client = AsyncMock(side_effect=register_client)

    # --- get_client ---
    async def get_client(client_id):
        return clients.get(client_id)

    auth.get_client = AsyncMock(side_effect=get_client)

    # --- authenticate_client ---
    def authenticate_client(client_id, client_secret, client_record):
        return client_record.get("client_secret") == client_secret

    auth.authenticate_client = MagicMock(side_effect=authenticate_client)

    # --- create_authorization_code ---
    async def create_authorization_code(client_id, redirect_uri, code_challenge, scope, provider, provider_state=None):
        code = str(uuid4())
        auth_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "scope": scope,
            "provider": provider,
            "client_state": provider_state,
        }
        return code

    auth.create_authorization_code = AsyncMock(side_effect=create_authorization_code)

    # --- get_authorization_code ---
    async def get_authorization_code(code):
        return auth_codes.get(code)

    auth.get_authorization_code = AsyncMock(side_effect=get_authorization_code)

    # --- set_authorization_code_user ---
    async def set_authorization_code_user(code, user_id, tenant_id, email=None):
        if code in auth_codes:
            auth_codes[code].update({"user_id": user_id, "tenant_id": tenant_id, "email": email})

    auth.set_authorization_code_user = AsyncMock(side_effect=set_authorization_code_user)

    # --- exchange_authorization_code ---
    async def exchange_authorization_code(code, client_id, code_verifier, redirect_uri):
        record = auth_codes.pop(code, None)
        if not record:
            raise ValueError("Invalid or expired authorization code")
        if record["client_id"] != client_id:
            raise ValueError("client_id mismatch")
        if record["redirect_uri"] != redirect_uri:
            raise ValueError("redirect_uri mismatch")
        # Verify PKCE
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed != record["code_challenge"]:
            raise ValueError("PKCE verification failed")
        return {
            "access_token": "test-access-token",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": "test-refresh-token",
        }

    auth.exchange_authorization_code = AsyncMock(side_effect=exchange_authorization_code)

    # --- handle_google_callback ---
    user = User(
        id=uuid4(),
        name="Test User",
        email="test@example.com",
        tenant_id=str(uuid4()),
    )
    tenant_id = user.tenant_id

    async def handle_google_callback(user_info):
        return user, tenant_id

    auth.handle_google_callback = AsyncMock(side_effect=handle_google_callback)

    # --- issue_tokens ---
    async def issue_tokens(u, tid):
        return {
            "access_token": "test-access-token",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token": "test-refresh-token",
        }

    auth.issue_tokens = AsyncMock(side_effect=issue_tokens)

    return auth, clients, auth_codes


def _make_app():
    """Create a FastAPI app with mocked auth for testing."""
    # Reset the global OAuth singleton so tests are isolated
    import p8.api.routers.auth as auth_module
    auth_module._oauth = None

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    mock_auth, clients, auth_codes = _make_mock_auth()
    settings = MagicMock()
    settings.api_base_url = "http://localhost:8000"
    settings.google_client_id = "test-google-id"
    settings.google_client_secret = "test-google-secret"
    settings.apple_client_id = None
    settings.mcp_auth_enabled = True
    settings.auth_secret_key = "test-secret-key-for-jwt"

    app.state.auth = mock_auth
    app.state.settings = settings

    from p8.api.routers.auth import router
    app.include_router(router, prefix="/auth")

    return app, mock_auth, clients, auth_codes


@pytest.fixture
def mcp_app():
    return _make_app()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpOAuthFlow:
    """Test the full MCP OAuth 2.1 flow."""

    def test_register_client(self, mcp_app):
        app, auth, clients, codes = mcp_app
        with TestClient(app) as client:
            resp = client.post("/auth/register", json={
                "redirect_uris": [CLIENT_REDIRECT_URI],
                "client_name": "Claude Desktop",
            })
            assert resp.status_code == 201
            data = resp.json()
            assert "client_id" in data
            assert "client_secret" in data
            assert data["redirect_uris"] == [CLIENT_REDIRECT_URI]

    def test_authorize_creates_code_and_redirects(self, mcp_app):
        """GET /auth/authorize with MCP params should create auth code and redirect to Google."""
        app, auth, clients, codes = mcp_app
        _, challenge = _make_pkce()

        with TestClient(app, follow_redirects=False) as client:
            # Register a client first
            reg = client.post("/auth/register", json={
                "redirect_uris": [CLIENT_REDIRECT_URI],
            }).json()

            # Mock the Google OAuth redirect
            with patch("p8.api.routers.auth._get_oauth") as mock_oauth:
                google_client = MagicMock()
                google_client.authorize_redirect = AsyncMock(
                    return_value=MagicMock(status_code=302, headers={"location": "https://accounts.google.com/o/oauth2/auth?..."})
                )
                oauth_instance = MagicMock()
                oauth_instance.google = google_client
                mock_oauth.return_value = oauth_instance

                resp = client.get("/auth/authorize", params={
                    "provider": "google",
                    "client_id": reg["client_id"],
                    "redirect_uri": CLIENT_REDIRECT_URI,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "client-state-abc",
                    "response_type": "code",
                })

            # Auth code should have been created
            auth.create_authorization_code.assert_called_once()
            assert len(codes) == 1

    def test_full_flow_register_authorize_callback_token(self, mcp_app):
        """Full end-to-end: register → authorize → callback → token exchange."""
        app, auth_mock, clients, codes = mcp_app
        verifier, challenge = _make_pkce()

        with TestClient(app, follow_redirects=False) as client:
            # 1. Register
            reg = client.post("/auth/register", json={
                "redirect_uris": [CLIENT_REDIRECT_URI],
            }).json()
            client_id = reg["client_id"]
            client_secret = reg["client_secret"]

            # 2. Authorize — need to mock the Google redirect
            with patch("p8.api.routers.auth._get_oauth") as mock_oauth:
                google_mock = MagicMock()

                # authorize_redirect returns a redirect to Google
                async def fake_authorize_redirect(request, callback_uri):
                    return MagicMock(
                        status_code=302,
                        headers={"location": "https://accounts.google.com/auth"},
                    )

                google_mock.authorize_redirect = AsyncMock(side_effect=fake_authorize_redirect)
                oauth_instance = MagicMock()
                oauth_instance.google = google_mock
                mock_oauth.return_value = oauth_instance

                resp = client.get("/auth/authorize", params={
                    "provider": "google",
                    "client_id": client_id,
                    "redirect_uri": CLIENT_REDIRECT_URI,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "my-state-123",
                    "response_type": "code",
                })

            # Grab the session cookie for the callback
            assert len(codes) == 1
            auth_code = list(codes.keys())[0]

            # 3. Callback — simulate Google returning with tokens
            with patch("p8.api.routers.auth._get_oauth") as mock_oauth:
                google_mock = MagicMock()

                async def fake_authorize_access_token(request):
                    return {"userinfo": GOOGLE_USER_INFO}

                google_mock.authorize_access_token = AsyncMock(side_effect=fake_authorize_access_token)
                oauth_instance = MagicMock()
                oauth_instance.google = google_mock
                mock_oauth.return_value = oauth_instance

                resp = client.get("/auth/callback/google")

            # Callback should redirect to the MCP client's redirect_uri with code
            assert resp.status_code == 302, f"Expected 302, got {resp.status_code}: {resp.text}"
            location = resp.headers["location"]
            assert CLIENT_REDIRECT_URI in location
            assert f"code={auth_code}" in location
            assert "state=my-state-123" in location

            # Auth code should now have user info attached
            code_record = codes[auth_code]
            assert code_record["user_id"] is not None
            assert code_record["email"] == "test@example.com"

            # 4. Token exchange
            resp = client.post("/auth/token", data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": CLIENT_REDIRECT_URI,
            })
            assert resp.status_code == 200, f"Token exchange failed: {resp.json()}"
            tokens = resp.json()
            assert "access_token" in tokens
            assert tokens["token_type"] == "bearer"

    def test_token_exchange_missing_params(self, mcp_app):
        """Token exchange should return clear error for missing params."""
        app, *_ = mcp_app
        with TestClient(app) as client:
            resp = client.post("/auth/token", data={
                "grant_type": "authorization_code",
                # Missing: code, client_id, code_verifier, redirect_uri
            })
            assert resp.status_code == 400
            data = resp.json()
            assert data["error"] == "invalid_request"
            assert "code" in data["error_description"]

    def test_token_exchange_bad_pkce(self, mcp_app):
        """Token exchange should fail with wrong code_verifier."""
        app, auth_mock, clients, codes = mcp_app
        _, challenge = _make_pkce()

        with TestClient(app) as client:
            # Register + create code manually
            reg = client.post("/auth/register", json={
                "redirect_uris": [CLIENT_REDIRECT_URI],
            }).json()

            # Directly inject a code
            code = str(uuid4())
            codes[code] = {
                "client_id": reg["client_id"],
                "redirect_uri": CLIENT_REDIRECT_URI,
                "code_challenge": challenge,
                "scope": "openid",
                "provider": "google",
                "client_state": None,
                "user_id": str(uuid4()),
                "tenant_id": str(uuid4()),
                "email": "test@example.com",
            }

            resp = client.post("/auth/token", data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
                "code_verifier": "wrong-verifier",
                "redirect_uri": CLIENT_REDIRECT_URI,
            })
            assert resp.status_code == 400
            assert resp.json()["error"] == "invalid_grant"
            assert "PKCE" in resp.json()["error_description"]
