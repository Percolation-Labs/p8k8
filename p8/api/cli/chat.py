"""p8 chat — interactive agent chat using the shared ChatController."""

from __future__ import annotations

import asyncio
from typing import Optional
from uuid import UUID

import typer

import p8.services.bootstrap as _svc
from p8.api.controllers.chat import ChatController
from p8.api.tools import init_tools

chat_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


async def _run_chat(
    agent: str | None,
    session_id: str | None,
    user_id: UUID | None,
):
    async with _svc.bootstrap_services() as (db, encryption, settings, file_service, *_rest):
        # Init tools so MCP toolsets work in-process
        init_tools(db, encryption)

        agent_name = agent or "general"
        controller = ChatController(db, encryption)

        # Resolve agent
        try:
            adapter = await controller.resolve_agent(agent_name, user_id=user_id)
            typer.echo(f"Agent: {adapter.schema.name}")
        except ValueError:
            typer.echo(f"Agent '{agent_name}' not found", err=True)
            raise typer.Exit(1)

        # Resolve or create session
        sid, session = await controller.get_or_create_session(
            UUID(session_id) if session_id else None,
            agent_name=agent_name,
            user_id=user_id,
            name_prefix="cli-chat",
        )
        if session_id:
            typer.echo(f"Resuming session {sid}")
        else:
            typer.echo(f"New session: {sid}")

        # Chat REPL
        typer.echo("Type your message (Ctrl+C to exit)\n")
        while True:
            try:
                user_input = input("you> ").strip()
            except (KeyboardInterrupt, EOFError):
                typer.echo("\nBye.")
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "\\q"):
                break

            # Full prepare → run cycle per turn (reloads history each turn)
            ctx = await controller.prepare(
                agent_name,
                session_id=sid,
                user_id=user_id,
                name_prefix="cli-chat",
            )

            turn = await controller.run_turn(ctx, user_input, user_id=user_id, background_compaction=False)
            typer.echo(f"assistant> {turn.assistant_text}")


@chat_app.callback()
def chat_command(
    session_id: Optional[str] = typer.Argument(
        None,
        help="Session UUID to resume. Omit to start a new session.",
    ),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent schema name"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Resume session by UUID (alias for positional arg)"),
    user_id: Optional[str] = typer.Option(
        None, "--user-id", "-u",
        help="User ID for context and message persistence.",
    ),
):
    """Interactive chat with an agent.

    Start a new session:   p8 chat
    Resume a session:      p8 chat SESSION_ID
    """
    # Positional arg takes priority, --session is an alias
    sid = session_id or session
    uid = UUID(user_id) if user_id else None
    asyncio.run(_run_chat(agent, sid, uid))
