"""MCP tools — callable from agents, MCP server, and REST endpoints.

Module-level db/encryption initialized by init_tools() during app lifespan.
Per-request user_id/session_id/security set via set_tool_context() using
ContextVars so they propagate correctly in concurrent async requests.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from uuid import UUID

from p8.services.database import Database
from p8.services.encryption import EncryptionService

if TYPE_CHECKING:
    from p8.api.security import SecurityContext

_db: Database | None = None
_encryption: EncryptionService | None = None

# Per-request context — set by chat router, CLI, dreaming worker, MCP middleware
_user_id_var: ContextVar[UUID | None] = ContextVar("tool_user_id", default=None)
_session_id_var: ContextVar[UUID | None] = ContextVar("tool_session_id", default=None)
_security_var: ContextVar[SecurityContext | None] = ContextVar("tool_security", default=None)


def init_tools(db: Database, encryption: EncryptionService) -> None:
    """Initialize tools with shared services. Called during app lifespan."""
    global _db, _encryption
    _db = db
    _encryption = encryption


def set_tool_context(
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    security: SecurityContext | None = None,
) -> None:
    """Set per-request user/session/security context for tool execution.

    Uses ContextVars so each async request gets its own values.
    Call this before running an agent or executing tools.
    """
    _user_id_var.set(user_id)
    _session_id_var.set(session_id)
    _security_var.set(security)


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("Tools not initialized — call init_tools() in lifespan")
    return _db


def get_encryption() -> EncryptionService:
    if _encryption is None:
        raise RuntimeError("Tools not initialized — call init_tools() in lifespan")
    return _encryption


def get_user_id() -> UUID | None:
    return _user_id_var.get()


def get_session_id() -> UUID | None:
    return _session_id_var.get()


def get_security() -> SecurityContext | None:
    return _security_var.get()


# ---------------------------------------------------------------------------
# Tool registry — direct Python callables for chained tool execution
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Callable[..., Any]] = {}


def _ensure_registry() -> None:
    if TOOL_REGISTRY:
        return
    from p8.api.tools.search import search
    from p8.api.tools.action import action
    from p8.api.tools.ask_agent import ask_agent
    from p8.api.tools.save_moments import save_moments
    from p8.api.tools.get_moments import get_moments
    from p8.api.tools.web_search import web_search
    from p8.api.tools.update_user_metadata import update_user_metadata
    from p8.api.tools.remind_me import remind_me

    TOOL_REGISTRY.update({
        "search": search,
        "action": action,
        "ask_agent": ask_agent,
        "save_moments": save_moments,
        "get_moments": get_moments,
        "web_search": web_search,
        "update_user_metadata": update_user_metadata,
        "remind_me": remind_me,
    })


def get_tool_fn(name: str) -> Callable[..., Any] | None:
    """Look up a tool function by name. Returns None if not found."""
    _ensure_registry()
    return TOOL_REGISTRY.get(name)
