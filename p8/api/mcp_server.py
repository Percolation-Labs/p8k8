"""FastMCP server — registers tools from api/tools/ and resources.

Mounted at ``/mcp`` on the FastAPI app using Streamable HTTP transport.

Auth architecture:

  Uses ``RemoteAuthProvider`` + ``JWTVerifier(HS256)`` — the MCP server
  is a **resource server** that validates the app's own HS256 JWTs but never
  issues tokens.  The main app (``/auth/*``) is the OAuth 2.1 Authorization
  Server.  MCP clients discover it via ``/.well-known/oauth-protected-resource``
  (auto-created by RemoteAuthProvider) → ``/.well-known/oauth-authorization-server``
  (served by auth router).

  One Google callback URL, one token system, one auth flow.
"""

from __future__ import annotations

import json
import logging

from fastmcp import FastMCP

from p8.api.tools import get_db, get_encryption, set_tool_context
from p8.api.tools.action import action
from p8.api.tools.ask_agent import ask_agent
from p8.api.tools.remind_me import remind_me
from p8.api.tools.get_moments import get_moments
# save_moments is not needed as an MCP tool — agents use structured output
# and workers persist moments directly (see DreamingHandler._persist_dream_moments)
# from p8.api.tools.save_moments import save_moments
from p8.api.tools.search import search
from p8.api.tools.update_user_metadata import update_user_metadata
from p8.api.tools.web_search import web_search
from p8.ontology.types import User
from p8.services.repository import Repository
from p8.settings import Settings, get_settings


async def user_profile() -> str:
    """Load current user's profile: name, email, content, metadata, tags."""
    from p8.api.tools import get_user_id
    user_id = get_user_id()
    if not user_id:
        return json.dumps({"error": "No authenticated user in context"})
    db = get_db()
    encryption = get_encryption()
    repo = Repository(User, db, encryption)
    results = await repo.find(user_id=user_id, limit=1)
    if not results:
        return json.dumps({"error": "User not found"})
    user = results[0]
    profile = {
        "user_id": str(user_id),
        "name": user.name,
        "email": user.email,
        "content": user.content,
        "metadata": user.metadata,
        "tags": user.tags,
    }
    return json.dumps(profile, default=str)


def _create_auth(settings: Settings):
    """Create RemoteAuthProvider that validates the app's own HS256 JWTs.

    The MCP server acts as a resource server — it validates tokens but does
    not issue them.  The main app at api_base_url is the OAuth 2.1
    Authorization Server (handles /auth/authorize, /auth/token, /auth/register).
    Works with any provider configured on the AS (Google, Apple, magic link).
    """
    if not settings.mcp_auth_enabled:
        return None

    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    jwt_verifier = JWTVerifier(
        public_key=settings.auth_secret_key,  # HS256 shared secret
        algorithm="HS256",
    )

    from pydantic import AnyHttpUrl
    return RemoteAuthProvider(
        token_verifier=jwt_verifier,
        authorization_servers=[AnyHttpUrl(settings.api_base_url)],
        base_url=settings.api_base_url,
        scopes_supported=["openid"],
        resource_name="p8",
    )


def create_mcp_server() -> FastMCP:
    """Create the FastMCP server with p8 tools and resources."""
    settings = get_settings()
    auth = _create_auth(settings)
    mcp = FastMCP(
        name="rem",
        instructions=settings.mcp_instructions,
        auth=auth,
    )

    # Register tools
    mcp.tool(name="search")(search)
    mcp.tool(name="action")(action)
    mcp.tool(name="ask_agent")(ask_agent)
    mcp.tool(name="remind_me")(remind_me)
    # save_moments commented out — agents use structured output in workers
    # mcp.tool(name="save_moments")(save_moments)
    mcp.tool(name="get_moments")(get_moments)
    mcp.tool(name="web_search")(web_search)
    mcp.tool(name="update_user_metadata")(update_user_metadata)

    # Also register user_profile as a tool — the Claude.ai MCP connector
    # only supports tools (not resources), so this ensures it works remotely.
    mcp.tool(name="get_user_profile")(user_profile)

    # Register resources (works over stdio, e.g. Claude Code)
    mcp.resource("user://profile")(user_profile)

    return mcp


# Singleton
_mcp_server: FastMCP | None = None


def get_mcp_server() -> FastMCP:
    """Get or create the MCP server singleton."""
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = create_mcp_server()
    return _mcp_server


class _ToolContextMiddleware:
    """ASGI middleware that resolves a SecurityContext and sets tool context.

    Resolution order mirrors resolve_security_context():
    1. Master key → MASTER
    2. Tenant key → TENANT
    3. JWT → USER (also extracts user_id for backward compat)
    4. x-user-id header → USER (dev mode)

    Proxies attribute access to the wrapped app so callers (e.g. lifespan)
    can access .router, .state, etc. transparently.
    """

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            import hmac
            from uuid import UUID
            from p8.api.security import PermissionLevel, SecurityContext

            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            bearer_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
            x_api_key = headers.get(b"x-api-key", b"").decode()
            settings = get_settings()

            ctx: SecurityContext | None = None
            user_id: UUID | None = None

            # 1. Master key
            if settings.master_key:
                for candidate in (bearer_token, x_api_key):
                    if candidate and hmac.compare_digest(candidate, settings.master_key):
                        ctx = SecurityContext.master()
                        break

            # 2. Tenant keys
            if not ctx and settings.tenant_keys:
                try:
                    tenant_map = json.loads(settings.tenant_keys)
                except (json.JSONDecodeError, TypeError):
                    tenant_map = {}
                for candidate in (bearer_token, x_api_key):
                    if candidate:
                        for tid, tkey in tenant_map.items():
                            if hmac.compare_digest(candidate, tkey):
                                ctx = SecurityContext(level=PermissionLevel.TENANT, tenant_id=tid)
                                break
                    if ctx:
                        break

            # 3. JWT → USER
            if not ctx and bearer_token:
                try:
                    import jwt as pyjwt
                    payload = pyjwt.decode(
                        bearer_token, settings.auth_secret_key, algorithms=["HS256"],
                    )
                    user_id = UUID(payload["sub"])
                    ctx = SecurityContext(
                        level=PermissionLevel.USER,
                        user_id=user_id,
                        tenant_id=payload.get("tenant_id", ""),
                        email=payload.get("email", ""),
                        provider=payload.get("provider", ""),
                        scopes=payload.get("scopes", []),
                    )
                except Exception:
                    pass

            # 4. x-user-id header (dev mode)
            if not ctx:
                raw = headers.get(b"x-user-id", b"").decode()
                if raw:
                    try:
                        user_id = UUID(raw)
                        ctx = SecurityContext(
                            level=PermissionLevel.USER,
                            user_id=user_id,
                            tenant_id=headers.get(b"x-tenant-id", b"").decode(),
                            email=headers.get(b"x-user-email", b"").decode(),
                            provider="header",
                        )
                    except ValueError:
                        pass

            # Extract user_id from context for backward compat
            if ctx and ctx.user_id:
                user_id = ctx.user_id

            set_tool_context(user_id=user_id, security=ctx)
        await self.app(scope, receive, send)


def get_mcp_app():
    """Get the MCP Streamable HTTP ASGI app for mounting."""
    mcp = get_mcp_server()
    inner = mcp.http_app(path="/")
    return _ToolContextMiddleware(inner)
