"""FastAPI dependency injection — shared service accessors."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from uuid import UUID

import jwt
from fastapi import HTTPException, Request

from p8.services.database import Database
from p8.services.encryption import EncryptionService


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_encryption(request: Request) -> EncryptionService:
    return request.app.state.encryption


async def require_api_key(request: Request) -> None:
    """Validate request auth: API key, valid JWT, or x-user-id header."""
    settings = request.app.state.settings
    if not settings.api_key:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # Accept raw API key
        if hmac.compare_digest(token, settings.api_key):
            return
        # Accept valid JWT access token (mobile clients)
        try:
            auth_svc = request.app.state.auth
            payload = auth_svc.verify_token(token)
            if payload.get("type") == "access":
                return
        except Exception:
            pass
    # Accept x-user-id header (dev/testing — identified but not JWT-secured)
    if request.headers.get("x-user-id"):
        return
    raise HTTPException(401, "Missing or invalid authentication")


@dataclass
class CurrentUser:
    user_id: UUID
    email: str
    tenant_id: str
    provider: str
    scopes: list[str] = field(default_factory=list)


def _extract_token(request: Request) -> str | None:
    """Extract JWT from Authorization header or access_token cookie."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get("access_token")


def _payload_to_user(payload: dict) -> CurrentUser:
    """Build CurrentUser from a verified JWT payload."""
    return CurrentUser(
        user_id=UUID(payload["sub"]),
        email=payload.get("email", ""),
        tenant_id=payload["tenant_id"],
        provider=payload.get("provider", ""),
        scopes=payload.get("scopes", []),
    )


async def get_current_user(request: Request) -> CurrentUser:
    """Require a valid JWT. Raises 401 on failure."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(401, "Not authenticated")
    auth = request.app.state.auth
    try:
        payload = auth.verify_token(token)
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if payload.get("type") != "access":
        raise HTTPException(401, "Invalid token type")
    return _payload_to_user(payload)


async def get_optional_user(request: Request) -> CurrentUser | None:
    """Try JWT auth, fall back to x-user-* headers, return None if neither."""
    token = _extract_token(request)
    if token:
        auth = request.app.state.auth
        try:
            payload = auth.verify_token(token)
            if payload.get("type") == "access":
                return _payload_to_user(payload)
        except jwt.PyJWTError:
            pass

    raw_user_id = request.headers.get("x-user-id")
    if raw_user_id:
        return CurrentUser(
            user_id=UUID(raw_user_id),
            email=request.headers.get("x-user-email", ""),
            tenant_id=request.headers.get("x-tenant-id", ""),
            provider="header",
        )
    return None
