"""p8 chat — interactive agent chat using the shared ChatController."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from uuid import UUID

import typer

import p8.services.bootstrap as _svc
from p8.api.controllers.chat import ChatController
from p8.api.tools import init_tools, set_tool_context

chat_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)

# Sage Whitfield — seeded test user
_DEFAULT_USER_ID = UUID("7d31eddf-7ff7-542a-982f-7522e7a3ec67")


def _enable_llm_debug_logging():
    """Enable debug logging on the OpenAI client to see actual LLM payloads.

    Logs the full json_data in "Request options" lines — system prompt,
    tools array, messages, model settings. Pipe stderr to a file to capture:

        uv run p8 chat --agent general --debug 2>payload.log
    """
    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
    logging.getLogger("openai._base_client").setLevel(logging.DEBUG)
    # Quiet noisy loggers that aren't the payload
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def _run_chat(
    agent: str | None,
    session_id: str | None,
    user_id: UUID | None,
    initial_message: str | None = None,
    debug: bool = False,
):
    if debug:
        _enable_llm_debug_logging()
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

        async def _send(text: str):
            ctx = await controller.prepare(
                agent_name,
                session_id=sid,
                user_id=user_id,
                name_prefix="cli-chat",
            )
            set_tool_context(user_id=user_id, session_id=ctx.session_id)
            import sys
            sys.stdout.write("\nassistant> ")
            sys.stdout.flush()
            async for delta in controller.run_turn_stream(ctx, text, user_id=user_id, background_compaction=False):
                sys.stdout.write(delta)
                sys.stdout.flush()
            sys.stdout.write("\n")
            sys.stdout.flush()

        # Send initial message if provided (e.g. `p8 chat "what is this"`)
        if initial_message:
            try:
                await _send(initial_message)
            except Exception as e:
                typer.echo(f"[error] {e}", err=True)

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

            try:
                await _send(user_input)
            except Exception as e:
                typer.echo(f"[error] {e}", err=True)


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


@chat_app.callback()
def chat_command(
    message_or_session: Optional[str] = typer.Argument(
        None,
        help="Session UUID to resume, or an initial message to send.",
    ),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Agent schema name"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Resume session by UUID"),
    user_id: Optional[str] = typer.Option(
        None, "--user-id", "-u",
        help="User ID (defaults to Sage Whitfield test user).",
    ),
    debug: bool = typer.Option(
        False, "--debug", "-d",
        help="Enable openai._base_client DEBUG logging to see actual LLM payloads.",
    ),
):
    """Interactive chat with an agent.

    Start a new session:   p8 chat
    Send a message:        p8 chat "what is this"
    Resume a session:      p8 chat SESSION_UUID
    Explicit session:      p8 chat --session SESSION_UUID "hello"
    View LLM payload:      p8 chat --debug 2>payload.log
    """
    # Determine if positional arg is a session UUID or an initial message
    sid = session  # --session flag always means session
    initial_message = None
    if message_or_session:
        if _is_uuid(message_or_session):
            sid = sid or message_or_session
        else:
            initial_message = message_or_session

    uid = UUID(user_id) if user_id else _DEFAULT_USER_ID
    asyncio.run(_run_chat(agent, sid, uid, initial_message=initial_message, debug=debug))
