"""JSONB and text helpers.

asyncpg sometimes delivers JSONB columns as raw strings instead of parsed
dicts/lists (e.g. when a default comes from the DB). These helpers normalise
that so callers don't need repetitive ``isinstance`` + ``json.loads`` blocks.

Examples::

    from p8.utils.parsing import ensure_parsed, extract_payload, truncate

    ensure_parsed('{"a": 1}')        # {"a": 1}
    ensure_parsed({"a": 1})          # {"a": 1}  (pass-through)
    extract_payload(task)            # parsed payload dict
    truncate("long text", 10)        # "long te..."
"""

from __future__ import annotations

import json


def ensure_parsed(value: str | dict | list | None, default=None) -> dict | list | None:
    """Parse a JSON string if needed; pass through dicts/lists unchanged.

    Args:
        value: A JSON string, already-parsed dict/list, or ``None``.
        default: Returned when *value* is ``None`` (default ``None``).

    Returns:
        Parsed Python object, or *default* if input is ``None``.

    Examples::

        ensure_parsed('{"a":1}')      # {"a": 1}
        ensure_parsed({"a": 1})       # {"a": 1}
        ensure_parsed(None, default=[])  # []
    """
    if value is None:
        return default  # type: ignore[no-any-return]
    if isinstance(value, str):
        return json.loads(value)  # type: ignore[no-any-return]
    return value


def extract_payload(task: dict) -> dict:
    """Extract and parse the ``payload`` field from a queue task dict.

    Args:
        task: A task dict (typically from ``task_queue``).

    Returns:
        Parsed payload as a dict (empty dict if missing or ``None``).

    Examples::

        extract_payload({"payload": '{"file_id": "abc"}'})  # {"file_id": "abc"}
        extract_payload({"payload": {"file_id": "abc"}})     # {"file_id": "abc"}
        extract_payload({})                                  # {}
    """
    payload = task.get("payload", {})
    if isinstance(payload, str):
        return json.loads(payload)  # type: ignore[no-any-return]
    return payload or {}


def truncate(text: str, max_len: int = 500, suffix: str = "...") -> str:
    """Truncate text to *max_len*, appending *suffix* if truncated.

    Args:
        text: Input string.
        max_len: Maximum length including the suffix.
        suffix: Appended when truncation occurs (default ``"..."``).

    Returns:
        Original text if short enough, otherwise truncated with suffix.

    Examples::

        truncate("hello", 10)        # "hello"
        truncate("hello world", 8)   # "hello..."
    """
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)] + suffix
