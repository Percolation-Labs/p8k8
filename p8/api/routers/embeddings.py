"""Embedding endpoints — batch processing and generation.

POST /embeddings/process  — Process a batch from the embedding queue.
                            Called by pg_cron (via pg_net) or cloud scheduler.
POST /embeddings/generate — Generate embeddings for arbitrary texts.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class GenerateRequest(BaseModel):
    texts: list[str]


class GenerateResponse(BaseModel):
    embeddings: list[list[float]]
    provider: str
    dimensions: int
    count: int


@router.post("/process")
async def process_queue(request: Request):
    """Claim and process one batch from the embedding queue.

    Designed to be called by pg_cron via pg_net every few seconds.
    Safe to call concurrently — uses FOR UPDATE SKIP LOCKED.
    """
    service = request.app.state.embedding_service
    result = await service.process_batch()
    return result


@router.post("/generate", response_model=GenerateResponse)
async def generate_embeddings(body: GenerateRequest, request: Request):
    """Generate embeddings for arbitrary texts.

    Uses the configured provider (local for dev, openai for production).
    """
    service = request.app.state.embedding_service
    embeddings = await service.embed_texts(body.texts)
    return GenerateResponse(
        embeddings=embeddings,
        provider=service.provider.provider_name,
        dimensions=service.provider.dimensions,
        count=len(embeddings),
    )
