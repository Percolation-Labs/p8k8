"""Embedding service — pluggable providers, batch processing from queue.

Providers:
  - local:     Deterministic hash-based embeddings (no API key, no deps). Use for testing.
  - fastembed: Local ONNX embeddings via FastEmbed (BAAI/bge-small-en-v1.5 default). Use for dev.
  - openai:    OpenAI text-embedding REST API via httpx (no SDK dependency). Use in production.

The embedding queue is populated by PostgreSQL triggers on entity tables.
Processing is triggered by pg_cron → pg_net calling POST /embeddings/process,
or by the optional background worker as a fallback.

In production, replace pg_cron with a cloud scheduler or dedicated worker process.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
from abc import ABC, abstractmethod

from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.settings import Settings
from p8.utils.ids import content_hash

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class EmbeddingProvider(ABC):
    """Generate embedding vectors from text."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed a list of texts. Returns one vector per input."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        ...


# ---------------------------------------------------------------------------
# Local provider — deterministic, zero dependencies
# ---------------------------------------------------------------------------


class LocalEmbeddingProvider(EmbeddingProvider):
    """Hash-based deterministic embeddings for development and testing.

    Same text always produces the same vector. Vectors are unit-normalized
    so cosine similarity works correctly — similar prefixes will have
    higher similarity than unrelated strings.

    Not suitable for production semantic search (no learned representations).
    """

    def __init__(self, dimensions: int = 1536):
        self._dimensions = dimensions

    @property
    def provider_name(self) -> str:
        return "local"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_embed(t) for t in texts]

    def _hash_embed(self, text: str) -> list[float]:
        """Expand SHA-512 hash into a deterministic float vector."""
        raw: list[float] = []
        seed = text.encode("utf-8")
        while len(raw) < self._dimensions:
            seed = hashlib.sha512(seed).digest()
            # Unpack as unsigned 16-bit ints (32 per 64-byte hash) to avoid NaN/Inf
            ints = struct.unpack("<32H", seed)
            raw.extend((i / 32767.5) - 1.0 for i in ints)

        vec = raw[: self._dimensions]
        # L2-normalize so cosine distance is meaningful
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


# ---------------------------------------------------------------------------
# FastEmbed provider — local ONNX inference
# ---------------------------------------------------------------------------


class FastEmbedProvider(EmbeddingProvider):
    """Local embeddings via FastEmbed (Qdrant). No API key needed.

    Uses ONNX runtime for fast CPU inference. Model downloads on first use.
    Default: BAAI/bge-small-en-v1.5 (384 dimensions).
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", dimensions: int = 384):
        self._model_name = model_name
        self._dimensions = dimensions
        self._model: object | None = None  # lazy init

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    @property
    def provider_name(self) -> str:
        return "fastembed"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()

        def _embed():
            return [vec.tolist() for vec in model.embed(texts)]

        return await asyncio.to_thread(_embed)


# ---------------------------------------------------------------------------
# OpenAI provider — REST via httpx (no SDK dependency)
# ---------------------------------------------------------------------------


class OpenAIRestProvider(EmbeddingProvider):
    """OpenAI text-embedding via REST API. Uses httpx, no openai SDK needed."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", dimensions: int = 1536):
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "input": texts,
                    "dimensions": self._dimensions,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["data"]]


# ---------------------------------------------------------------------------
# Embedding service — batch queue processing
# ---------------------------------------------------------------------------


class EmbeddingService:
    """Processes the embedding_queue using the configured provider.

    Called by:
      - POST /embeddings/process (triggered by pg_cron or cloud scheduler)
      - Background worker (optional fallback)
    """

    def __init__(
        self,
        db: Database,
        provider: EmbeddingProvider,
        encryption: EncryptionService,
        batch_size: int = 20,
    ):
        self.db = db
        self.provider = provider
        self.encryption = encryption
        self.batch_size = batch_size

    async def process_batch(self) -> dict:
        """Claim and process one batch from the embedding queue.

        Returns summary: {"processed": N, "skipped": N, "failed": N}
        """
        batch = await self._claim_batch()
        if not batch:
            return {"processed": 0, "skipped": 0, "failed": 0}

        # Extract content for each item
        items_with_text: list[tuple[dict, str, str]] = []  # (item, text, text_hash)
        skipped = 0

        for item in batch:
            text = await self._extract_content(item)
            if not text:
                await self._remove_from_queue(item)
                skipped += 1
                continue
            text = await self._maybe_decrypt(item["table_name"], item["entity_id"], text)
            text_hash = content_hash(text)

            # Check content-hash cache — skip if embedding already exists for this content
            existing_hash = await self.db.fetchval(
                f"SELECT content_hash FROM embeddings_{item['table_name']}"
                " WHERE entity_id = $1 AND field_name = $2 AND provider = $3",
                item["entity_id"],
                item["field_name"],
                self.provider.provider_name,
            )
            if existing_hash == text_hash:
                await self._remove_from_queue(item)
                skipped += 1
                continue

            items_with_text.append((item, text, text_hash))

        if not items_with_text:
            return {"processed": 0, "skipped": skipped, "failed": 0}

        # Batch embed all texts at once
        texts = [t for _, t, _ in items_with_text]
        try:
            embeddings = await self.provider.embed(texts)
        except Exception as e:
            log.error("Batch embedding failed: %s", e)
            for item, _, _ in items_with_text:
                await self._fail_item(item, str(e))
            return {"processed": 0, "skipped": skipped, "failed": len(items_with_text)}

        # Store each embedding
        processed = 0
        failed = 0
        for (item, _, text_hash), embedding in zip(items_with_text, embeddings):
            try:
                await self.db.execute(
                    "SELECT upsert_embedding($1, $2, $3, $4::vector, $5, $6)",
                    item["table_name"],
                    item["entity_id"],
                    item["field_name"],
                    str(embedding),
                    self.provider.provider_name,
                    text_hash,
                )
                processed += 1
                log.info(
                    "Embedded %s/%s/%s via %s",
                    item["table_name"],
                    item["entity_id"],
                    item["field_name"],
                    self.provider.provider_name,
                )
            except Exception as e:
                log.warning("Failed to store embedding for %s/%s: %s", item["table_name"], item["entity_id"], e)
                await self._fail_item(item, str(e))
                failed += 1

        return {"processed": processed, "skipped": skipped, "failed": failed}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for arbitrary texts (utility method)."""
        return await self.provider.embed(texts)

    async def backfill(self, table: str) -> int:
        """Queue all rows in a table that lack embeddings. Returns count queued."""
        from p8.ontology.types import EMBEDDABLE_TABLES

        if table not in EMBEDDABLE_TABLES:
            raise ValueError(
                f"'{table}' is not an embeddable table. "
                f"Valid: {', '.join(EMBEDDABLE_TABLES)}"
            )

        await self.db.execute(
            f"INSERT INTO embedding_queue (table_name, entity_id, field_name, status)"
            f" SELECT '{table}', e.id, 'content', 'pending'"
            f" FROM {table} e"
            f" LEFT JOIN embeddings_{table} emb"
            f"   ON emb.entity_id = e.id AND emb.field_name = 'content'"
            f" WHERE e.deleted_at IS NULL AND emb.id IS NULL"
            f" ON CONFLICT (table_name, entity_id, field_name) DO NOTHING",
        )
        count = await self.db.fetchval(
            "SELECT COUNT(*) FROM embedding_queue"
            " WHERE table_name = $1 AND status = 'pending'",
            table,
        )
        return count  # type: ignore[no-any-return]

    # --- internal helpers ---

    async def _claim_batch(self) -> list[dict]:
        rows = await self.db.fetch(
            """UPDATE embedding_queue
               SET status = 'processing', attempts = attempts + 1
               WHERE id IN (
                   SELECT id FROM embedding_queue
                   WHERE status = 'pending'
                   ORDER BY created_at ASC
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED
               )
               RETURNING table_name, entity_id, field_name""",
            self.batch_size,
        )
        return [dict(r) for r in rows]

    async def _extract_content(self, item: dict) -> str | None:
        return await self.db.fetchval(  # type: ignore[no-any-return]
            "SELECT content_for_embedding($1, $2, $3)",
            item["table_name"],
            item["entity_id"],
            item["field_name"],
        )

    async def _remove_from_queue(self, item: dict):
        await self.db.execute(
            "DELETE FROM embedding_queue"
            " WHERE table_name=$1 AND entity_id=$2 AND field_name=$3",
            item["table_name"],
            item["entity_id"],
            item["field_name"],
        )

    async def _fail_item(self, item: dict, error: str):
        await self.db.execute(
            "SELECT fail_embedding($1, $2, $3, $4)",
            item["table_name"],
            item["entity_id"],
            item["field_name"],
            error,
        )

    async def _maybe_decrypt(self, table: str, entity_id, text: str) -> str:
        """Attempt to decrypt content if it belongs to an encrypted table."""
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        row = await self.db.fetchrow(f"SELECT tenant_id FROM {table} WHERE id = $1", entity_id)
        tenant_id = row["tenant_id"] if row else None
        if not tenant_id:
            return text

        await self.encryption.get_dek(tenant_id)
        cached = self.encryption._dek_cache.get(tenant_id)
        if not cached:
            return text

        try:
            raw = base64.b64decode(text)
            nonce, ct = raw[:12], raw[12:]
            aad = f"{tenant_id}:{entity_id}".encode()
            dek = cached[0]
            assert isinstance(dek, bytes)
            return AESGCM(dek).decrypt(nonce, ct, aad).decode()
        except Exception:
            return text  # not encrypted


# ---------------------------------------------------------------------------
# Background worker — optional fallback when pg_cron is not available
# ---------------------------------------------------------------------------


class EmbeddingWorker:
    """Async polling worker. Use when pg_cron + pg_net is not configured.

    For production, prefer pg_cron → POST /embeddings/process or a cloud scheduler.
    """

    def __init__(self, service: EmbeddingService, poll_interval: float = 2.0):
        self.service = service
        self.poll_interval = poll_interval
        self._running = False

    async def run(self):
        self._running = True
        log.info("Embedding worker started (poll_interval=%.1fs)", self.poll_interval)
        while self._running:
            try:
                result = await self.service.process_batch()
                if result["processed"] == 0 and result["failed"] == 0:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Embedding worker error")
                await asyncio.sleep(5)
        log.info("Embedding worker stopped")

    async def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def parse_embedding_model(embedding_model: str) -> tuple[str, str | None]:
    """Parse 'provider:model_name' → (provider, model_name).

    Examples:
        "local"                            → ("local", None)
        "fastembed:BAAI/bge-small-en-v1.5" → ("fastembed", "BAAI/bge-small-en-v1.5")
        "openai:text-embedding-3-small"    → ("openai", "text-embedding-3-small")
    """
    if ":" in embedding_model:
        provider, model_name = embedding_model.split(":", 1)
        return provider, model_name
    return embedding_model, None


def create_provider(settings: Settings) -> EmbeddingProvider:
    """Create the configured embedding provider from settings.embedding_model."""
    provider, model_name = parse_embedding_model(settings.embedding_model)

    if provider == "openai":
        return OpenAIRestProvider(
            api_key=settings.openai_api_key,
            model=model_name or "text-embedding-3-small",
            dimensions=settings.embedding_dimensions,
        )
    if provider == "fastembed":
        return FastEmbedProvider(
            model_name=model_name or "BAAI/bge-small-en-v1.5",
            dimensions=settings.embedding_dimensions,
        )
    # Default: local hash-based (testing only)
    return LocalEmbeddingProvider(dimensions=settings.embedding_dimensions)
