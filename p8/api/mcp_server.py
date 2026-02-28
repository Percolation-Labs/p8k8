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
from typing import Any

from fastmcp import FastMCP

log = logging.getLogger(__name__)

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
from p8.api.tools.plots import save_plot
from p8.api.tools.files import get_file, get_file_resource, resolve_data_path
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


def create_mcp_server(*, stdio: bool = False) -> FastMCP:
    """Create the FastMCP server with p8 tools and resources.

    Args:
        stdio: When True (``p8 mcp`` CLI), also register platoon_read_file
               for local filesystem access.  Over HTTP this tool is omitted —
               use ``get_file`` with uploaded file IDs instead.
    """
    settings = get_settings()
    auth = _create_auth(settings)
    mcp = FastMCP(
        name="rem",
        instructions=settings.mcp_instructions,
        auth=auth,
    )

    # ── Platoon commerce analytics tools ────────────────────────────────
    # Register tools directly (not via mount) so we can control which are
    # available per transport.  forecast/optimize are always available;
    # read_file is stdio-only (local filesystem).
    try:
        from platoon.mcp import controllers as platoon_ctl

        if stdio:
            @mcp.tool(name="platoon_read_file")
            async def _platoon_read_file(path: str, head: int = 0) -> dict[str, Any]:
                """Read a CSV or text file from the local filesystem.

                Only available in local (stdio) mode. For cloud access, upload
                files to Percolate and use get_file instead.

                Args:
                    path: File path to read.
                    head: If > 0, return only the first N rows/lines.
                """
                return platoon_ctl.read_file(path, head=head)  # type: ignore[no-any-return]

        @mcp.tool(name="platoon_forecast")
        async def _platoon_forecast(
            data_path: str,
            product_id: str | None = None,
            horizon: int = 14,
            method: str = "auto",
            season_length: int = 7,
            holdout: int = 30,
        ) -> dict[str, Any]:
            """Run demand forecasting on a CSV time series.

            CSV must have columns: date, product_id, units_sold.

            Args:
                data_path: Path to daily demand CSV file.
                    Can be a local file path or an uploaded file ID (UUID).
                product_id: Which product to forecast. Picks highest-volume if omitted.
                horizon: Days ahead to forecast (default 14). Must be >= 1.
                method: auto, moving_average, exponential_smoothing, croston, arima, ets, theta.
                season_length: Seasonal period in days (default 7).
                holdout: Days to hold out for accuracy eval (default 30).
            """
            resolved = await resolve_data_path(data_path)
            return platoon_ctl.forecast(  # type: ignore[no-any-return]
                resolved, product_id=product_id, horizon=horizon,
                method=method, season_length=season_length, holdout=holdout,
            )

        @mcp.tool(name="platoon_optimize")
        async def _platoon_optimize(
            data_path: str,
            product_id: str | None = None,
            orders_path: str | None = None,
            inventory_path: str | None = None,
            service_level: float = 0.95,
        ) -> dict[str, Any]:
            """Run inventory optimization on product data.

            Computes EOQ, safety stock, reorder point, ABC class, stockout risk,
            plus price, cost, daily_revenue, restock_cost, and days_of_stock.

            Args:
                data_path: Path to products CSV (product_id, sku, cost, price, base_daily_demand, lead_time_days).
                    Can be a local file path or an uploaded file ID (UUID).
                product_id: Analyze a single product. If omitted, analyzes all.
                orders_path: Optional orders CSV for ABC classification.
                    Can be a local file path or an uploaded file ID (UUID).
                inventory_path: Optional inventory CSV for current stock levels.
                    Can be a local file path or an uploaded file ID (UUID).
                service_level: Target service level (0-1 exclusive, default 0.95).
            """
            resolved = await resolve_data_path(data_path)
            resolved_orders = await resolve_data_path(orders_path) if orders_path else None
            resolved_inventory = await resolve_data_path(inventory_path) if inventory_path else None
            return platoon_ctl.optimize(  # type: ignore[no-any-return]
                resolved, product_id=product_id, orders_path=resolved_orders,
                inventory_path=resolved_inventory, service_level=service_level,
            )

        @mcp.tool(name="platoon_detect_anomalies")
        async def _platoon_detect_anomalies(
            data_path: str,
            product_id: str | None = None,
            method: str = "zscore",
            window: int = 30,
            threshold: float = 2.5,
        ) -> dict[str, Any]:  # type: ignore[no-any-return]
            """Detect spikes and drops in demand time series using rolling-window statistics.

            Args:
                data_path: Path to daily demand CSV file.
                    Can be a local file path or an uploaded file ID (UUID).
                product_id: Which product to analyze. Picks highest-volume if omitted.
                method: Detection method — "zscore" or "iqr".
                window: Rolling window size in days (default 30).
                threshold: Z-score threshold (default 2.5, zscore method only).
            """
            resolved = await resolve_data_path(data_path)
            return platoon_ctl.detect_anomalies(  # type: ignore[no-any-return]
                resolved, product_id=product_id, method=method,
                window=window, threshold=threshold,
            )

        @mcp.tool(name="platoon_basket_analysis")
        async def _platoon_basket_analysis(
            orders_path: str,
            min_support: float = 0.01,
            min_confidence: float = 0.3,
            max_rules: int = 50,
        ) -> dict[str, Any]:  # type: ignore[no-any-return]
            """Find frequently-bought-together association rules from order data.

            Args:
                orders_path: Path to orders CSV (order_id, product_id columns required).
                    Can be a local file path or an uploaded file ID (UUID).
                min_support: Minimum support threshold (default 0.01).
                min_confidence: Minimum confidence threshold (default 0.3).
                max_rules: Maximum rules to return (default 50).
            """
            resolved = await resolve_data_path(orders_path)
            return platoon_ctl.basket_analysis(  # type: ignore[no-any-return]
                resolved, min_support=min_support,
                min_confidence=min_confidence, max_rules=max_rules,
            )

        @mcp.tool(name="platoon_cashflow")
        async def _platoon_cashflow(
            data_path: str,
            demand_path: str,
            inventory_path: str | None = None,
            horizon: int = 30,
            product_id: str | None = None,
        ) -> dict[str, Any]:  # type: ignore[no-any-return]
            """Project daily revenue, COGS, and reorder costs over a horizon.

            Args:
                data_path: Path to products CSV.
                    Can be a local file path or an uploaded file ID (UUID).
                demand_path: Path to daily demand CSV.
                    Can be a local file path or an uploaded file ID (UUID).
                inventory_path: Optional inventory CSV for reorder simulation.
                    Can be a local file path or an uploaded file ID (UUID).
                horizon: Days to project forward (default 30).
                product_id: Analyze a single product. If omitted, analyzes all.
            """
            resolved_data = await resolve_data_path(data_path)
            resolved_demand = await resolve_data_path(demand_path)
            resolved_inv = await resolve_data_path(inventory_path) if inventory_path else None
            return platoon_ctl.cashflow(  # type: ignore[no-any-return]
                resolved_data, resolved_demand,
                inventory_path=resolved_inv, horizon=horizon,
                product_id=product_id,
            )

        @mcp.tool(name="platoon_schedule")
        async def _platoon_schedule(
            demand_path: str,
            staff_path: str,
            shift_hours: float = 8.0,
            min_coverage: float = 1.0,
            horizon_days: int = 7,
            product_id: str | None = None,
        ) -> dict[str, Any]:  # type: ignore[no-any-return]
            """Assign staff to shifts based on demand signal and availability.

            Args:
                demand_path: Path to daily demand CSV.
                    Can be a local file path or an uploaded file ID (UUID).
                staff_path: Path to staff CSV (name, hourly_rate, max_hours, available_days).
                    Can be a local file path or an uploaded file ID (UUID).
                shift_hours: Hours per shift (default 8).
                min_coverage: Minimum coverage fraction per slot (default 1.0).
                horizon_days: Days to schedule (default 7).
                product_id: Filter demand to a single product if specified.
            """
            resolved_demand = await resolve_data_path(demand_path)
            resolved_staff = await resolve_data_path(staff_path)
            return platoon_ctl.schedule(  # type: ignore[no-any-return]
                resolved_demand, resolved_staff,
                shift_hours=shift_hours, min_coverage=min_coverage,
                horizon_days=horizon_days, product_id=product_id,
            )

        log.info(
            "Registered Platoon tools (forecast, optimize, detect_anomalies, "
            "basket_analysis, cashflow, schedule%s)",
            ", read_file" if stdio else "",
        )
    except ImportError:
        log.info("Platoon not installed — commerce tools skipped")

    # ── Core tools ──────────────────────────────────────────────────────
    mcp.tool(name="search")(search)
    mcp.tool(name="action")(action)
    mcp.tool(name="ask_agent")(ask_agent)
    mcp.tool(name="remind_me")(remind_me)
    mcp.tool(name="get_moments")(get_moments)
    mcp.tool(name="web_search")(web_search)
    mcp.tool(name="update_user_metadata")(update_user_metadata)
    mcp.tool(name="save_plot")(save_plot)

    # File access — always available (reads uploaded files from S3/DB)
    mcp.tool(name="get_file")(get_file)

    # Also register user_profile as a tool — the Claude.ai MCP connector
    # only supports tools (not resources), so this ensures it works remotely.
    mcp.tool(name="get_user_profile")(user_profile)

    # ── Resources (works over stdio, e.g. Claude Code) ─────────────────
    mcp.resource("user://profile")(user_profile)
    mcp.resource("files://{file_id}")(get_file_resource)

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
