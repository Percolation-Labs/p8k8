"""Auth endpoints — tenant & user management, OAuth, magic link, sessions."""

from __future__ import annotations

import json
import logging
from uuid import UUID

import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from p8.api.deps import CurrentUser, get_current_user
from p8.ontology.types import StorageGrant
from p8.services.repository import Repository

logger = logging.getLogger(__name__)

router = APIRouter()

# Lazily initialized OAuth instance (needs app settings at runtime)
_oauth: OAuth | None = None


def _get_oauth(request: Request) -> OAuth:
    """Get or create the Authlib OAuth instance from app settings."""
    global _oauth
    if _oauth is not None:
        return _oauth

    settings = request.app.state.settings
    _oauth = OAuth()

    # Google — OIDC auto-discovery
    if settings.google_client_id:
        _oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    # Apple — manual configuration (no full OIDC discovery)
    if settings.apple_client_id:
        auth = request.app.state.auth
        _oauth.register(
            name="apple",
            client_id=settings.apple_client_id,
            client_secret=auth.generate_apple_client_secret(),
            authorize_url="https://appleid.apple.com/auth/authorize",
            access_token_url="https://appleid.apple.com/auth/token",
            client_kwargs={"scope": "name email", "response_mode": "form_post"},
            jwks_uri="https://appleid.apple.com/auth/keys",
            token_endpoint_auth_method="client_secret_post",
        )

    return _oauth


def _set_token_cookies(response: JSONResponse | RedirectResponse, tokens: dict) -> None:
    """Set HttpOnly cookies for access + refresh tokens."""
    response.set_cookie(
        "access_token",
        tokens["access_token"],
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=tokens["expires_in"],
    )
    response.set_cookie(
        "refresh_token",
        tokens["refresh_token"],
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
        max_age=30 * 24 * 3600,  # 30 days
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateTenantRequest(BaseModel):
    name: str
    encryption_mode: str = "platform"
    own_key: bool = False


class ConfigureEncryptionRequest(BaseModel):
    mode: str  # platform | client | sealed | disabled
    public_key_pem: str | None = None  # PEM string for sealed mode (tenant-provided)


class CreateUserRequest(BaseModel):
    name: str
    email: str
    provider: str | None = None
    provider_user_id: str | None = None


class SignupRequest(BaseModel):
    name: str
    email: str
    encryption_mode: str = "platform"


class MagicLinkRequest(BaseModel):
    email: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str | None = None  # from body, or cookie fallback


class RevokeTokenRequest(BaseModel):
    refresh_token: str | None = None


# ---------------------------------------------------------------------------
# Tenant endpoints (unchanged)
# ---------------------------------------------------------------------------


@router.post("/tenants", status_code=201)
async def create_tenant(body: CreateTenantRequest, request: Request):
    auth = request.app.state.auth
    tenant = await auth.create_tenant(
        body.name, encryption_mode=body.encryption_mode, own_key=body.own_key
    )
    return tenant.model_dump(mode="json")


@router.get("/tenants/{tenant_id}")
async def get_tenant(tenant_id: UUID, request: Request):
    auth = request.app.state.auth
    tenant = await auth.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return tenant.model_dump(mode="json")


@router.post("/tenants/{tenant_id}/encryption")
async def configure_encryption(
    tenant_id: UUID, body: ConfigureEncryptionRequest, request: Request
):
    auth = request.app.state.auth
    public_key_pem = body.public_key_pem.encode() if body.public_key_pem else None
    result = await auth.configure_tenant_encryption(
        tenant_id, body.mode, public_key_pem=public_key_pem
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


# ---------------------------------------------------------------------------
# User endpoints (unchanged)
# ---------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/users", status_code=201)
async def create_user(tenant_id: UUID, body: CreateUserRequest, request: Request):
    auth = request.app.state.auth
    # Verify tenant exists
    tenant = await auth.get_tenant(tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    user = await auth.create_user(
        body.name,
        body.email,
        tenant_id=str(tenant_id),
        provider=body.provider,
        provider_user_id=body.provider_user_id,
    )
    return user.model_dump(mode="json")


@router.get("/tenants/{tenant_id}/users")
async def list_users(tenant_id: UUID, request: Request, limit: int = 50):
    auth = request.app.state.auth
    users = await auth.find_users(tenant_id=str(tenant_id), limit=limit)
    return [u.model_dump(mode="json") for u in users]


# ---------------------------------------------------------------------------
# Signup (1:1 personal tenant + user)
# ---------------------------------------------------------------------------


@router.post("/signup", status_code=201)
async def signup(body: SignupRequest, request: Request):
    auth = request.app.state.auth
    tenant, user = await auth.create_personal_tenant(
        body.name, body.email, encryption_mode=body.encryption_mode
    )
    return {
        "tenant": tenant.model_dump(mode="json"),
        "user": user.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# OAuth 2.1 — authorize + callback
# ---------------------------------------------------------------------------


@router.get("/authorize")
async def oauth_authorize(request: Request, provider: str):
    """Redirect to OAuth provider (google or apple)."""
    oauth = _get_oauth(request)
    client = getattr(oauth, provider, None)
    if client is None:
        raise HTTPException(400, f"Unknown or unconfigured provider: {provider}")

    settings = request.app.state.settings
    redirect_uri = f"{settings.api_base_url}/auth/callback/{provider}"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/callback/{provider}")
@router.post("/callback/{provider}")
async def oauth_callback(request: Request, provider: str):
    """Handle OAuth callback, issue tokens, set cookies, redirect."""
    # MCP OAuth flows use the same Google callback URL but go through
    # /mcp/authorize (FastMCP), not /auth/authorize (authlib).
    # Detect by checking if authlib stored state in the session.
    state = request.query_params.get("state", "")
    if state and not any(k.startswith(f"_state_{provider}_") for k in request.session):
        from starlette.responses import RedirectResponse
        return RedirectResponse(f"/mcp/auth/callback/{provider}?{request.url.query}")

    oauth = _get_oauth(request)
    client = getattr(oauth, provider, None)
    if client is None:
        raise HTTPException(400, f"Unknown provider: {provider}")

    auth = request.app.state.auth
    settings = request.app.state.settings

    token_data = await client.authorize_access_token(request)

    if provider == "google":
        user_info = token_data.get("userinfo") or {}
        if not user_info:
            # Fallback: fetch from userinfo endpoint
            resp = await client.get("https://openidconnect.googleapis.com/v1/userinfo")
            user_info = resp.json()
        user, tenant_id = await auth.handle_google_callback(user_info)

    elif provider == "apple":
        # Apple sends id_token in the token response
        id_token_claims = token_data.get("userinfo") or {}
        # Apple sends user info as form POST param on first auth only
        form = await request.form()
        user_json = form.get("user")
        user_info = json.loads(user_json) if user_json else None
        user, tenant_id = await auth.handle_apple_callback(id_token_claims, user_info)

    else:
        raise HTTPException(400, f"Unsupported provider: {provider}")

    tokens = await auth.issue_tokens(user, tenant_id)

    # Redirect to frontend with cookies set
    redirect_url = f"{settings.api_base_url}/"
    response = RedirectResponse(url=redirect_url, status_code=302)
    _set_token_cookies(response, tokens)
    return response


# ---------------------------------------------------------------------------
# Mobile — server-relayed OAuth (browser-based, no native SDK)
# ---------------------------------------------------------------------------

_MOBILE_APP_SCHEME = "remapp"


@router.get("/mobile/authorize/google-drive")
async def mobile_drive_authorize(request: Request, token: str | None = None):
    """Initiate Google OAuth with Drive scope (opt-in file sync).

    Accepts the user's access token as a ?token= query param (since the
    browser opening this URL doesn't have the app's Authorization header).
    The user identity is verified and stored in the session so the callback
    can associate the Drive grant with the right user.
    """
    # Verify the token passed as query param
    if not token:
        raise HTTPException(401, "Missing token — open this URL from the app")
    auth_svc = request.app.state.auth
    try:
        payload = auth_svc.verify_token(token)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if payload.get("type") != "access":
        raise HTTPException(401, "Invalid token type")

    request.session["drive_connect_user_id"] = payload["sub"]
    request.session["drive_connect_tenant_id"] = payload["tenant_id"]

    oauth = _get_oauth(request)
    client = getattr(oauth, "google", None)
    if client is None:
        raise HTTPException(400, "Google OAuth not configured")

    settings = request.app.state.settings
    redirect_uri = f"{settings.api_base_url}/auth/mobile/callback/google-drive"
    return await client.authorize_redirect(
        request,
        redirect_uri,
        scope="openid email profile https://www.googleapis.com/auth/drive.readonly",
        access_type="offline",
        prompt="consent",
    )


@router.get("/mobile/callback/google-drive")
@router.post("/mobile/callback/google-drive")
async def mobile_drive_callback(request: Request):
    """Handle Google Drive OAuth callback — store refresh token, redirect to app."""
    user_id = request.session.pop("drive_connect_user_id", None)
    tenant_id = request.session.pop("drive_connect_tenant_id", None)
    if not user_id:
        raise HTTPException(400, "Missing session — please retry Drive connect from the app")

    oauth = _get_oauth(request)
    client = getattr(oauth, "google", None)
    if client is None:
        raise HTTPException(400, "Google OAuth not configured")

    auth = request.app.state.auth
    settings = request.app.state.settings
    token_data = await client.authorize_access_token(request)

    google_refresh = token_data.get("refresh_token")
    if not google_refresh:
        return RedirectResponse(
            url=f"{_MOBILE_APP_SCHEME}://drive-callback?status=error&reason=no_refresh_token",
            status_code=302,
        )

    # Store in StorageGrant
    grants_repo = Repository(StorageGrant, auth.db, auth.encryption)
    grant = StorageGrant(
        user_id_ref=UUID(user_id),
        tenant_id=tenant_id,
        provider="google-drive",
        status="active",
        metadata={
            "refresh_token": google_refresh,
            "scopes": token_data.get("scope", ""),
        },
    )
    await grants_repo.upsert(grant)

    return RedirectResponse(
        url=f"{_MOBILE_APP_SCHEME}://drive-callback?status=connected",
        status_code=302,
    )


@router.get("/mobile/authorize/{provider}")
async def mobile_oauth_authorize(request: Request, provider: str):
    """Initiate OAuth from mobile app. Opens Google in browser, callbacks redirect to app."""
    oauth = _get_oauth(request)
    client = getattr(oauth, provider, None)
    if client is None:
        raise HTTPException(400, f"Unknown or unconfigured provider: {provider}")

    settings = request.app.state.settings
    redirect_uri = f"{settings.api_base_url}/auth/mobile/callback/{provider}"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/mobile/callback/{provider}")
@router.post("/mobile/callback/{provider}")
async def mobile_oauth_callback(request: Request, provider: str):
    """Handle OAuth callback for mobile, redirect to app deep link with tokens."""
    oauth = _get_oauth(request)
    client = getattr(oauth, provider, None)
    if client is None:
        raise HTTPException(400, f"Unknown provider: {provider}")

    auth = request.app.state.auth

    token_data = await client.authorize_access_token(request)

    if provider == "google":
        user_info = token_data.get("userinfo") or {}
        if not user_info:
            resp = await client.get("https://openidconnect.googleapis.com/v1/userinfo")
            user_info = resp.json()
        user, tenant_id = await auth.handle_google_callback(user_info)

    elif provider == "apple":
        # Authlib may store decoded id_token claims under "userinfo" or as
        # top-level keys in token_data; also try decoding id_token directly
        id_token_claims = token_data.get("userinfo") or {}
        if not id_token_claims.get("sub"):
            # Try extracting from the raw id_token JWT
            raw_id_token = token_data.get("id_token")
            if raw_id_token and isinstance(raw_id_token, str):
                import jwt as pyjwt
                id_token_claims = pyjwt.decode(raw_id_token, options={"verify_signature": False})
        if not id_token_claims.get("sub"):
            # Some Authlib versions put claims at top level
            id_token_claims = {k: v for k, v in token_data.items()
                               if k in ("sub", "email", "email_verified", "aud", "iss")}
        form = await request.form()
        user_json = form.get("user")
        user_info = json.loads(user_json) if user_json else None
        user, tenant_id = await auth.handle_apple_callback(id_token_claims, user_info)

    else:
        raise HTTPException(400, f"Unsupported provider: {provider}")

    tokens = await auth.issue_tokens(user, tenant_id)

    from urllib.parse import urlencode

    params = urlencode({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "user_id": str(user.id),
        "tenant_id": tenant_id,
        "email": user.email or "",
        "name": user.name or "",
    })
    return RedirectResponse(
        url=f"{_MOBILE_APP_SCHEME}://auth-callback?{params}",
        status_code=302,
    )


@router.post("/mobile/google/drive-disconnect")
async def mobile_google_drive_disconnect(request: Request):
    """Revoke the stored Google Drive grant for the current user."""
    current = await get_current_user(request)
    auth = request.app.state.auth

    # Find and revoke active google-drive grants for this user
    rows = await auth.db.fetch(
        "UPDATE storage_grants SET status = 'revoked', metadata = '{}'::jsonb"
        " WHERE user_id_ref = $1 AND provider = 'google-drive' AND status = 'active'"
        " RETURNING id",
        UUID(current.user_id),
    )

    return {"status": "disconnected", "revoked": len(rows)}


@router.get("/mobile/google/drive-status")
async def mobile_google_drive_status(request: Request):
    """Check whether the current user has an active Google Drive grant."""
    current = await get_current_user(request)
    auth = request.app.state.auth

    row = await auth.db.fetchrow(
        "SELECT id, status FROM storage_grants"
        " WHERE user_id_ref = $1 AND provider = 'google-drive' AND status = 'active'"
        " LIMIT 1",
        UUID(current.user_id),
    )

    return {"connected": row is not None}


# ---------------------------------------------------------------------------
# .well-known/oauth-authorization-server (RFC 8414)
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def well_known_oauth(request: Request):
    settings = request.app.state.settings
    base = settings.api_base_url
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/auth/authorize",
        "token_endpoint": f"{base}/auth/token",
        "revocation_endpoint": f"{base}/auth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
    }


# ---------------------------------------------------------------------------
# Token refresh + revoke
# ---------------------------------------------------------------------------


@router.post("/token")
async def token_refresh(request: Request, body: RefreshTokenRequest = RefreshTokenRequest()):
    """Exchange a refresh token for a new access + refresh pair (rotation)."""
    auth = request.app.state.auth

    # Get refresh token from body or cookie
    refresh_token = body.refresh_token or request.cookies.get("refresh_token")

    if not refresh_token:
        raise HTTPException(400, "No refresh token provided")

    try:
        tokens = await auth.refresh_tokens(refresh_token)
    except jwt.PyJWTError as e:
        raise HTTPException(401, str(e))

    response = JSONResponse(tokens)
    _set_token_cookies(response, tokens)
    return response


@router.post("/revoke")
async def token_revoke(request: Request, body: RevokeTokenRequest = RevokeTokenRequest()):
    """Revoke a refresh token."""
    auth = request.app.state.auth

    refresh_token = body.refresh_token or request.cookies.get("refresh_token")

    if refresh_token:
        try:
            payload = auth.verify_token(refresh_token)
            await auth.revoke_refresh_jti(payload.get("jti", ""))
        except jwt.PyJWTError:
            pass  # Already invalid, nothing to revoke

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Magic link
# ---------------------------------------------------------------------------


@router.post("/magic-link")
async def send_magic_link(body: MagicLinkRequest, request: Request):
    """Send a magic link email. Always returns 200 (no email leak)."""
    auth = request.app.state.auth
    try:
        await auth.send_magic_link(body.email)
    except Exception:
        logger.exception("Failed to send magic link")
    # Always 200 — don't reveal whether email exists
    return {"status": "ok"}


@router.get("/verify")
async def verify_magic_link(request: Request, token: str):
    """Verify a magic link token, issue session tokens, redirect with cookies."""
    auth = request.app.state.auth
    settings = request.app.state.settings

    try:
        user, tenant_id = await auth.verify_magic_link(token)
    except jwt.PyJWTError as e:
        raise HTTPException(400, f"Invalid or expired magic link: {e}")

    tokens = await auth.issue_tokens(user, tenant_id)

    redirect_url = f"{settings.api_base_url}/"
    response = RedirectResponse(url=redirect_url, status_code=302)
    _set_token_cookies(response, tokens)
    return response


# ---------------------------------------------------------------------------
# Session — /me + /logout
# ---------------------------------------------------------------------------


@router.get("/me")
async def me(request: Request):
    """Return the authenticated user's profile."""
    current = await get_current_user(request)
    auth = request.app.state.auth
    uid = current.user_id if isinstance(current.user_id, UUID) else UUID(current.user_id)
    user = await auth.get_user(uid, tenant_id=current.tenant_id)
    if not user:
        raise HTTPException(404, "User not found")
    return {
        "user": user.model_dump(mode="json"),
        "tenant_id": current.tenant_id,
        "provider": current.provider,
    }


class PatchUserRequest(BaseModel):
    name: str | None = None
    interests: list[str] | None = None
    activity_level: str | None = None
    content: str | None = None
    devices: list[dict] | None = None


@router.patch("/me")
async def patch_me(request: Request, body: PatchUserRequest):
    """Update the authenticated user's profile.

    Use this to register/update device tokens, change settings, etc.
    The mobile app should call this after obtaining a push token:

        PATCH /auth/me
        {"devices": [{"platform": "apns", "token": "...", "device_name": "iPhone"}]}
    """
    current = await get_current_user(request)
    uid = current.user_id if isinstance(current.user_id, UUID) else UUID(current.user_id)

    # Build SQL SET clauses for non-None fields (direct SQL avoids encryption
    # round-trip which can fail if the master key rotated between pod restarts).
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    db = request.app.state.db
    import json as _json
    set_clauses = []
    params = [uid]  # $1 = user id
    for i, (field, value) in enumerate(updates.items(), start=2):
        if field == "devices":
            set_clauses.append(f"{field} = ${i}::jsonb")
            params.append(_json.dumps(value))
        elif field == "interests":
            set_clauses.append(f"{field} = ${i}::text[]")
            params.append(value)
        else:
            set_clauses.append(f"{field} = ${i}")
            params.append(value)
    set_clauses.append("updated_at = CURRENT_TIMESTAMP")

    sql = f"UPDATE users SET {', '.join(set_clauses)} WHERE id = $1 AND deleted_at IS NULL RETURNING id, name, devices"
    row = await db.fetchrow(sql, *params)
    if not row:
        raise HTTPException(404, "User not found")

    return {"user": dict(row)}


@router.post("/logout")
async def logout(request: Request):
    """Clear session cookies and revoke refresh token."""
    auth = request.app.state.auth

    # Revoke refresh token if present
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        try:
            payload = auth.verify_token(refresh_token)
            await auth.revoke_refresh_jti(payload.get("jti", ""))
        except jwt.PyJWTError:
            pass

    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token", path="/auth")
    return response
