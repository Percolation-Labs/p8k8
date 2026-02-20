"""Shared service bootstrap — Settings, DB, KMS, Encryption init.

Used by both the API lifespan (api/main.py) and CLI commands (api/cli/).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from p8.services.content import ContentService
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.files import FileService
from p8.services.kms import LocalFileKMS, VaultTransitKMS
from p8.settings import Settings

# Maps Settings attributes → env vars expected by third-party SDKs.
_SDK_ENV_MAPPINGS = {"openai_api_key": "OPENAI_API_KEY"}


def create_kms(settings: Settings, db: Database):
    """Select and return the configured KMS provider."""
    if settings.kms_provider == "vault":
        return VaultTransitKMS(
            settings.kms_vault_url, settings.kms_vault_token,
            settings.kms_vault_transit_key, db,
        )
    return LocalFileKMS(settings.kms_local_keyfile, db)


async def _ensure_system_key(
    encryption: EncryptionService, db: Database, settings: Settings,
) -> None:
    """Ensure the system tenant DEK exists, retrying once on stale key."""
    try:
        await encryption.ensure_system_key()
    except Exception:
        await db.execute("DELETE FROM tenant_keys WHERE tenant_id = $1", settings.system_tenant_id)
        encryption._dek_cache.pop(settings.system_tenant_id, None)
        await encryption.ensure_system_key()


def _export_api_keys(settings: Settings) -> None:
    """Bridge P8_-prefixed keys to standard env vars for SDKs (e.g. OPENAI_API_KEY)."""
    for attr, env_name in _SDK_ENV_MAPPINGS.items():
        value = getattr(settings, attr, "")
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value


@asynccontextmanager
async def bootstrap_services(*, include_embeddings: bool = False):
    """Shared service init for API lifespan and CLI commands.

    Yields a 6-tuple:
        (db, encryption, settings, file_service, content_service, embedding_service)

    embedding_service is None unless include_embeddings=True.
    """
    settings = Settings()
    _export_api_keys(settings)
    db = Database(settings)
    await db.connect()

    kms = create_kms(settings, db)
    encryption = EncryptionService(
        kms, system_tenant_id=settings.system_tenant_id, cache_ttl=settings.dek_cache_ttl
    )
    await _ensure_system_key(encryption, db, settings)

    file_service = FileService(settings)
    content_service = ContentService(
        db=db, encryption=encryption, file_service=file_service, settings=settings
    )

    embedding_service = None
    if include_embeddings:
        from p8.services.embeddings import EmbeddingService, create_provider

        provider = create_provider(settings)
        embedding_service = EmbeddingService(
            db, provider, encryption, batch_size=settings.embedding_batch_size
        )

    # Configure OTel console exporter when debug_llm is enabled
    if settings.debug_llm:
        _setup_debug_llm_tracing()

    try:
        yield db, encryption, settings, file_service, content_service, embedding_service
    finally:
        await db.close()


def _setup_debug_llm_tracing() -> None:
    """Configure OpenTelemetry ConsoleSpanExporter for LLM request debugging.

    When P8_DEBUG_LLM=true, this prints all OTel spans (including
    pydantic-ai model requests) to stderr. Shows the full model input:
    system prompt, instructions, messages, tools, and settings.
    """
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
        from opentelemetry.trace import set_tracer_provider

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        set_tracer_provider(provider)
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "P8_DEBUG_LLM=true but opentelemetry-sdk not installed. "
            "Install with: uv add opentelemetry-sdk"
        )
