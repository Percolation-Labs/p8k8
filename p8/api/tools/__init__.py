"""MCP tools — callable from agents, MCP server, and REST endpoints.

Module-level state is initialized by init_tools() during app lifespan.
"""

from __future__ import annotations

from p8.services.database import Database
from p8.services.encryption import EncryptionService

_db: Database | None = None
_encryption: EncryptionService | None = None


def init_tools(db: Database, encryption: EncryptionService) -> None:
    """Initialize tools with shared services. Called during app lifespan."""
    global _db, _encryption
    _db = db
    _encryption = encryption


def get_db() -> Database:
    assert _db is not None, "Tools not initialized — call init_tools() in lifespan"
    return _db


def get_encryption() -> EncryptionService:
    assert _encryption is not None, "Tools not initialized — call init_tools() in lifespan"
    return _encryption
