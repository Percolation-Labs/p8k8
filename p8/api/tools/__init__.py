"""MCP tools — callable from agents, MCP server, and REST endpoints.

Module-level state is initialized by init_tools() during app lifespan.
"""

from __future__ import annotations

from uuid import UUID

from p8.services.database import Database
from p8.services.encryption import EncryptionService

_db: Database | None = None
_encryption: EncryptionService | None = None
_user_id: UUID | None = None
_session_id: UUID | None = None


def init_tools(
    db: Database,
    encryption: EncryptionService,
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
) -> None:
    """Initialize tools with shared services. Called during app lifespan."""
    global _db, _encryption, _user_id, _session_id
    _db = db
    _encryption = encryption
    _user_id = user_id
    _session_id = session_id


def get_db() -> Database:
    assert _db is not None, "Tools not initialized — call init_tools() in lifespan"
    return _db


def get_encryption() -> EncryptionService:
    assert _encryption is not None, "Tools not initialized — call init_tools() in lifespan"
    return _encryption


def get_user_id() -> UUID | None:
    return _user_id


def get_session_id() -> UUID | None:
    return _session_id
