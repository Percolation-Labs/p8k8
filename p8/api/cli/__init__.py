"""CLI entry point — Typer app with async service bootstrap.

Service lifecycle delegated to services.bootstrap.bootstrap_services().
All subcommands share the same bootstrap_services() context manager.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import typer

from p8.services.bootstrap import bootstrap_services

app = typer.Typer(name="p8", no_args_is_help=True)


@asynccontextmanager
async def async_services():
    """Bootstrap services for CLI commands. Thin wrapper around bootstrap_services()."""
    async with bootstrap_services() as services:
        yield services


# Register subcommands
from p8.api.cli.chat import chat_app  # noqa: E402
from p8.api.cli.migrate import migrate_app  # noqa: E402
from p8.api.cli.query import query_app  # noqa: E402
from p8.api.cli.schema import schema_app  # noqa: E402
from p8.api.cli.serve import serve_app  # noqa: E402
from p8.api.cli.upsert import upsert_app  # noqa: E402

app.add_typer(serve_app, name="serve")
app.add_typer(migrate_app, name="migrate")
app.add_typer(query_app, name="query")
app.add_typer(upsert_app, name="upsert")
app.add_typer(schema_app, name="schema")
app.add_typer(chat_app, name="chat")

from p8.api.cli.moments import moments_app  # noqa: E402

app.add_typer(moments_app, name="moments")

from p8.api.cli.encryption import encryption_app  # noqa: E402

app.add_typer(encryption_app, name="encryption")

from p8.api.cli.mcp import mcp_app  # noqa: E402

app.add_typer(mcp_app, name="mcp")

from p8.api.cli.verify_links import verify_links_app  # noqa: E402

app.add_typer(verify_links_app, name="verify-links")

from p8.api.cli.admin import admin_app  # noqa: E402

app.add_typer(admin_app, name="admin")

from p8.api.cli.db import db_app  # noqa: E402

app.add_typer(db_app, name="db")

from p8.api.cli.dreaming import register_dream_command  # noqa: E402

register_dream_command(app)
