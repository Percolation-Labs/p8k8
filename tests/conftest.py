"""Shared test configuration — env vars and helpers only."""

from __future__ import annotations

import os
from uuid import UUID

import pytest

# Force test-safe settings before any module imports Settings()
os.environ["P8_EMBEDDING_MODEL"] = "local"
os.environ["P8_EMBEDDING_WORKER_ENABLED"] = "false"
os.environ["P8_API_KEY"] = ""  # disable auth middleware in tests
os.environ["P8_OTEL_ENABLED"] = "false"  # disable OTEL in tests

from p8.ontology.base import deterministic_id  # noqa: E402
from p8.settings import Settings  # noqa: E402


def det_id(table: str, name: str) -> UUID:
    """Deterministic UUID5 from table+name for idempotent test inserts."""
    return deterministic_id(table, name)


@pytest.fixture(scope="session")
def settings():
    return Settings()
