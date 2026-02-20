"""p8 serve — start the API server."""

from __future__ import annotations

from typing import Optional

import typer

serve_app = typer.Typer(no_args_is_help=False, invoke_without_command=True)


@serve_app.callback()
def serve_command(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8000, "--port", "-p", help="Bind port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)"),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes"),
):
    """Start the p8 API server (uvicorn)."""
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers,
    )
