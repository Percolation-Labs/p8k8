"""UUID generation and content hashing helpers.

Centralises short-ID and hash patterns used across the codebase so callers
don't need to inline ``uuid4().hex[:8]`` or ``hashlib.sha256(…)`` everywhere.

Re-exports ``deterministic_id`` and ``P8_NAMESPACE`` from
``p8.ontology.base`` for convenience.

Examples::

    from p8.utils.ids import short_id, content_hash, deterministic_id

    short_id("worker-")          # 'worker-a1b2c3d4'
    content_hash("hello world")  # sha256 hex digest
    deterministic_id("schemas", "my-agent")
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from p8.ontology.base import P8_NAMESPACE, deterministic_id  # noqa: F401 — re-export


def short_id(prefix: str = "", length: int = 8) -> str:
    """Generate a short random hex ID.

    Args:
        prefix: String prepended to the hex portion.
        length: Number of hex characters (default 8 → 4 bytes of randomness).

    Returns:
        ``f"{prefix}{uuid4().hex[:length]}"``

    Examples::

        short_id("chatcmpl-")  # 'chatcmpl-a1b2c3d4'
        short_id("worker-")    # 'worker-e5f6a7b8'
        short_id()             # 'c9d0e1f2'
    """
    return f"{prefix}{uuid4().hex[:length]}"


def content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8 encoded text.

    Args:
        text: Input string.

    Returns:
        64-character lowercase hex digest.

    Examples::

        content_hash("hello")  # 2cf24dba5fb0a30e26e83b2ac5b9e29e...
    """
    return hashlib.sha256(text.encode()).hexdigest()
