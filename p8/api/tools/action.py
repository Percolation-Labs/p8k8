"""action tool — emit typed action events for SSE streaming and UI updates."""

from __future__ import annotations

from typing import Any


async def action(
    type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit a typed action event for SSE streaming and UI updates.

    Action Types:
        - "observation": Record metadata (confidence, sources, reasoning)
        - "elicit": Request additional information from the user
        - "delegate": Signal delegation to another agent

    Args:
        type: Action type (observation, elicit, delegate)
        payload: Action-specific data

    Returns:
        Action result confirming the event was emitted
    """
    result: dict[str, Any] = {
        "_action_event": True,
        "action_type": type,
        "status": "success",
    }
    if payload:
        result["payload"] = payload
    return result
