"""FastMCP server — registers tools from api/tools/ and resources.

Mounted at /mcp on the FastAPI app using Streamable HTTP transport.
Google OAuth is enabled when P8_GOOGLE_CLIENT_ID is configured.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from p8.api.tools import get_db, get_encryption
from p8.api.tools.action import action
from p8.api.tools.ask_agent import ask_agent
from p8.api.tools.search import search
from p8.ontology.types import User
from p8.services.repository import Repository
from p8.settings import Settings


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
    """Create Google OAuth provider if credentials are configured."""
    if not settings.google_client_id:
        return None

    from fastmcp.server.auth.providers.google import GoogleProvider

    return GoogleProvider(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=f"{settings.api_base_url}/mcp",
    )


def create_mcp_server() -> FastMCP:
    """Create the FastMCP server with p8 tools and resources."""
    settings = Settings()
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


def get_mcp_app():
    """Get the MCP Streamable HTTP ASGI app for mounting."""
    mcp = get_mcp_server()
    return mcp.http_app(path="/")
