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
from p8.ontology.types import User
from p8.services.repository import Repository
from p8.settings import Settings, get_settings


async def user_profile(user_id: str) -> str:
    """Load user profile: name, email, content, metadata, tags."""
    from uuid import UUID
    db = get_db()
    encryption = get_encryption()
    repo = Repository(User, db, encryption)
    try:
        uid = UUID(user_id)
        results = await repo.find(user_id=uid, limit=1)
    except ValueError:
        results = []
    if not results:
        results = await repo.find(filters={"email": user_id}, limit=1)
    if not results:
        return json.dumps({"error": "User not found"})
    user = results[0]
    profile = {
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

    # Register resources
    mcp.resource("user://profile/{user_id}")(user_profile)

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
    """ASGI middleware that extracts user_id from Bearer JWT and sets tool context.

    This ensures MCP tool functions can access user_id via get_user_id()
    without requiring it as an explicit parameter.
    Proxies attribute access to the wrapped app so callers (e.g. lifespan)
    can access .router, .state, etc. transparently.
    """

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                try:
                    import jwt as pyjwt
                    from uuid import UUID
                    token = auth_header[7:]
                    settings = get_settings()
                    payload = pyjwt.decode(
                        token, settings.auth_secret_key, algorithms=["HS256"],
                    )
                    user_id = UUID(payload["sub"])
                    set_tool_context(user_id=user_id)
                except Exception:
                    pass  # Auth validation handled by FastMCP's RemoteAuthProvider
        await self.app(scope, receive, send)


def get_mcp_app():
    """Get the MCP Streamable HTTP ASGI app for mounting."""
    mcp = get_mcp_server()
    inner = mcp.http_app(path="/")
    return _ToolContextMiddleware(inner)
