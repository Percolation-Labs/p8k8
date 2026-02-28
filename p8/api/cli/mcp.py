"""p8 mcp — run MCP server over stdio for local development."""

from __future__ import annotations

import typer

mcp_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


@mcp_app.callback()
def mcp_command():
    """Run the p8 MCP server over stdio transport."""
    import asyncio

    from p8.api.mcp_server import create_mcp_server
    from p8.api.tools import init_tools, set_tool_context
    from p8.services.bootstrap import bootstrap_services

    async def _run():
        async with bootstrap_services() as (
            db, encryption, settings, file_service, content_service, embedding_service, queue_service,
        ):
            # Default user for auth-disabled stdio mode (Jamie Rivera test user)
            default_user_id = None
            if not settings.mcp_auth_enabled:
                from p8.ontology.base import deterministic_id
                default_user_id = deterministic_id("users", "user1@example.com")

            init_tools(db, encryption)
            set_tool_context(user_id=default_user_id)
            mcp = create_mcp_server(stdio=True)
            await mcp.run_async(transport="stdio")

    asyncio.run(_run())
