"""GET /whoami — minimal status check confirming the CI/deploy pipeline works."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def whoami():
    """Return a small JSON payload confirming the API is reachable."""
    return {"status": "ok", "message": "hello from the claude action pipeline"}
