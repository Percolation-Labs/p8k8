"""FastAPI application — lifespan, middleware, routers, and MCP mount."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from p8.api.deps import require_api_key
from p8.api.mcp_server import get_mcp_app
from p8.api.tools import init_tools
from p8.services.auth import AuthService
from p8.services.bootstrap import bootstrap_services
from p8.services.embeddings import EmbeddingWorker
from p8.services.notifications import NotificationService
from p8.services.stripe import StripeService


async def _heal_reminder_jobs(db) -> None:
    """Fix legacy reminder cron jobs that hardcode a URL instead of using the GUC.

    Jobs should reference current_setting('p8.internal_api_url') so that
    changing the URL in postgresql.conf fixes all jobs at once.
    """
    import logging
    import re

    log = logging.getLogger("p8.startup")
    rows = await db.fetch(
        "SELECT jobid, jobname, schedule, command FROM cron.job "
        "WHERE jobname LIKE 'reminder-%' "
        "AND command NOT LIKE '%current_setting%internal_api_url%'"
    )
    if not rows:
        return

    log.info("Healing %d reminder job(s) with hardcoded URLs", len(rows))

    url_expr = "current_setting('p8.internal_api_url', true) || '/notifications/send'"
    headers_expr = (
        "jsonb_build_object("
        "'Authorization', 'Bearer ' || current_setting('p8.api_key', true), "
        "'Content-Type', 'application/json')"
    )

    for r in rows:
        name, schedule, cmd = r["jobname"], r["schedule"], r["command"]
        body_match = re.search(r"body := '(.+?)'::jsonb", cmd, re.DOTALL)
        if not body_match:
            log.warning("Could not parse body from job %s — skipping", name)
            continue

        payload = body_match.group(1)
        is_one_time = "cron.unschedule" in cmd

        new_cmd = (
            f"SELECT net.http_post("
            f"url := {url_expr}, "
            f"headers := {headers_expr}, "
            f"body := '{payload}'::jsonb"
            f");"
        )
        if is_one_time:
            new_cmd += f" SELECT cron.unschedule('{name}');"

        await db.execute("SELECT cron.unschedule($1)", name)
        await db.execute("SELECT cron.schedule($1, $2, $3)", name, schedule, new_cmd)
        log.info("Healed %s — now uses GUC-based URL", name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with bootstrap_services(include_embeddings=True) as (
        db, encryption, settings, file_service, content_service, embedding_service, queue_service,
    ):
        if settings.otel_enabled:
            from p8.agentic.otel import setup_instrumentation
            setup_instrumentation()

        # Self-heal: fix any reminder cron jobs with hardcoded URLs
        try:
            await _heal_reminder_jobs(db)
        except Exception:
            import logging
            logging.getLogger("p8.startup").warning(
                "Could not heal reminder jobs (pg_cron may not be available)", exc_info=True
            )

        worker_task = None
        if settings.embedding_worker_enabled:
            worker = EmbeddingWorker(embedding_service, poll_interval=settings.embedding_poll_interval)
            worker_task = asyncio.create_task(worker.run())
            app.state.worker = worker

        auth = AuthService(db, encryption, settings)
        init_tools(db, encryption)

        app.state.db = db
        app.state.settings = settings
        app.state.encryption = encryption
        app.state.embedding_service = embedding_service
        app.state.auth = auth
        app.state.file_service = file_service
        app.state.content_service = content_service
        app.state.queue_service = queue_service
        app.state.stripe_service = StripeService(db, settings) if settings.stripe_secret_key else None

        # Push notifications (gated on at least one platform being configured)
        notification_service = None
        if settings.apns_bundle_id or settings.fcm_project_id:
            notification_service = NotificationService(db, settings)
        app.state.notification_service = notification_service

        # StreamableHTTPSessionManager.run() is single-use, so create a fresh
        # MCP ASGI app each lifespan cycle and swap it into the existing mount.
        fresh_mcp = get_mcp_app()
        for route in app.routes:
            if hasattr(route, "path") and route.path == "/mcp":
                route.app = fresh_mcp  # type: ignore[attr-defined]
                break
        app.state.mcp_app = fresh_mcp
        async with fresh_mcp.router.lifespan_context(fresh_mcp):
            yield

        if notification_service:
            await notification_service.close()

        if worker_task:
            await app.state.worker.stop()
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
    # https_only + same_site="none" needed for Apple's form_post cross-origin callback
    _is_production = boot_settings.api_base_url.startswith("https")
    app.add_middleware(
        SessionMiddleware,
        secret_key=boot_settings.auth_secret_key,
        https_only=_is_production,
        same_site="none" if _is_production else "lax",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    # Trust X-Forwarded-Proto/For from reverse proxy so request.url uses https://
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=["*"])

    from p8.api.routers import admin, auth, chat, content, embeddings, moments, notifications, payments, query, resources, schemas, share

    # Protected routers — require API key when P8_API_KEY is set
    api_key_dep = [Depends(require_api_key)]
    app.include_router(schemas.router, prefix="/schemas", tags=["schemas"], dependencies=api_key_dep)
    app.include_router(query.router, prefix="/query", tags=["query"], dependencies=api_key_dep)
    app.include_router(chat.router, prefix="/chat", tags=["chat"], dependencies=api_key_dep)
    app.include_router(content.router, prefix="/content", tags=["content"], dependencies=api_key_dep)
    app.include_router(moments.router, prefix="/moments", tags=["moments"], dependencies=api_key_dep)
    app.include_router(admin.router, prefix="/admin", tags=["admin"], dependencies=api_key_dep)
    app.include_router(embeddings.router, prefix="/embeddings", tags=["embeddings"], dependencies=api_key_dep)
    app.include_router(share.router, prefix="/share", tags=["share"], dependencies=api_key_dep)
    app.include_router(resources.router, prefix="/resources", tags=["resources"], dependencies=api_key_dep)
    app.include_router(notifications.router, prefix="/notifications", tags=["notifications"], dependencies=api_key_dep)
    # Billing — JWT-only auth (mobile clients), no API key dep
    app.include_router(payments.router, prefix="/billing", tags=["billing"])
    app.include_router(payments.webhook_router, prefix="/billing", tags=["billing"])

    # Auth router — open (handles OAuth callbacks, token exchange)
    app.include_router(auth.router, prefix="/auth", tags=["auth"])

    # Root health check (matches Dockerfile HEALTHCHECK path)
    @app.get("/health")
    async def root_health():
        providers = []
        if boot_settings.google_client_id:
            providers.append("google")
        if boot_settings.apple_client_id:
            providers.append("apple")
        providers.append("magic_link")  # always available
        return {
            "status": "ok",
            "auth": {
                "mcp_auth_enabled": boot_settings.mcp_auth_enabled,
                "providers": providers,
                "authorization_server": f"{boot_settings.api_base_url}/.well-known/oauth-authorization-server",
                "protected_resource": f"{boot_settings.api_base_url}/.well-known/oauth-protected-resource/mcp",
            },
        }

    # .well-known/oauth-authorization-server for the app's own OAuth (mobile/web)
    @app.get("/.well-known/oauth-authorization-server")
    async def root_well_known(request: Request):
        return await auth.well_known_oauth(request)

    # MCP server — created and mounted once; lifespan initializes session manager.
    app.state.mcp_app = get_mcp_app()
    app.mount("/mcp", app.state.mcp_app)

    # RFC 9728 — Protected Resource Metadata must be at the root level.
    # RemoteAuthProvider creates these routes inside the MCP sub-app, but they
    # need to be at /.well-known/oauth-protected-resource/mcp (root-level).
    if boot_settings.mcp_auth_enabled:
        @app.get("/.well-known/oauth-protected-resource/mcp")
        async def mcp_protected_resource(request: Request):
            base = boot_settings.api_base_url
            return {
                "resource": f"{base}/mcp/",
                "authorization_servers": [base],
                "scopes_supported": ["openid"],
                "bearer_methods_supported": ["header"],
                "resource_name": "p8",
            }

    return app


app = create_app()
