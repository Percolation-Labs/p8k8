"""FastAPI application with database lifespan, embedding worker, and MCP server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from p8.api.mcp_server import get_mcp_app
from p8.api.tools import init_tools
from p8.services.auth import AuthService
from p8.services.bootstrap import bootstrap_services
from p8.services.embeddings import EmbeddingWorker


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with bootstrap_services(include_embeddings=True) as (
        db, encryption, settings, file_service, content_service, embedding_service,
    ):
        # Optional background worker (fallback when pg_cron + pg_net is not available)
        worker = None
        worker_task = None
        if settings.embedding_worker_enabled:
            worker = EmbeddingWorker(embedding_service, poll_interval=settings.embedding_poll_interval)
            worker_task = asyncio.create_task(worker.run())

        auth = AuthService(db, encryption, settings)

        # Initialize tools with shared services (used by MCP server and agents)
        init_tools(db, encryption)

        app.state.db = db
        app.state.settings = settings
        app.state.encryption = encryption
        app.state.embedding_service = embedding_service
        app.state.auth = auth
        app.state.worker = worker
        app.state.file_service = file_service
        app.state.content_service = content_service

        yield

        if worker:
            await worker.stop()
        if worker_task:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    from p8.settings import Settings

    boot_settings = Settings()

    app = FastAPI(title="p8", version="0.1.0", lifespan=lifespan)

    # SessionMiddleware required for OAuth state during redirects
    app.add_middleware(SessionMiddleware, secret_key=boot_settings.auth_secret_key)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    from p8.api.routers import admin, auth, chat, content, embeddings, moments, query, schemas, share

    app.include_router(schemas.router, prefix="/schemas", tags=["schemas"])
    app.include_router(query.router, prefix="/query", tags=["query"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(content.router, prefix="/content", tags=["content"])
    app.include_router(moments.router, prefix="/moments", tags=["moments"])
    app.include_router(admin.router, prefix="/admin", tags=["admin"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(embeddings.router, prefix="/embeddings", tags=["embeddings"])
    app.include_router(share.router, prefix="/share", tags=["share"])

    # Mount .well-known at app root (in addition to /auth prefix)
    @app.get("/.well-known/oauth-authorization-server")
    async def root_well_known(request: Request):
        return await auth.well_known_oauth(request)

    # Mount MCP server at /mcp (Streamable HTTP)
    app.mount("/mcp", get_mcp_app())

    return app


app = create_app()
