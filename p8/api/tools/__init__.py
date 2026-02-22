"""MCP tools — callable from agents, MCP server, and REST endpoints.

Module-level db/encryption initialized by init_tools() during app lifespan.
Per-request user_id/session_id/security set via set_tool_context() using
ContextVars so they propagate correctly in concurrent async requests.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING
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
