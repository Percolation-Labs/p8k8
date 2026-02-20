"""Configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://p8:p8_dev@localhost:5488/p8"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # Embeddings — model format is "provider:model_name"
    #   openai:text-embedding-3-small      — OpenAI REST API, 1536d (default)
    #   local                              — hash-based zero vectors (tests only, set in conftest.py)
    embedding_model: str = "openai:text-embedding-3-small"
    openai_api_key: str = ""
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 20
    embedding_poll_interval: float = 2.0
    embedding_worker_enabled: bool = True  # False when pg_cron + pg_net handles scheduling

    # API (used by pg_cron pg_net to call back into the embedding processor)
    api_base_url: str = "http://localhost:8000"

    # MCP server instructions (system prompt for MCP clients)
    mcp_instructions: str = (
        "REM is a multi-modal knowledge base. Use `search` for queries, "
        "`action` to emit events, and `ask_agent` for delegation."
    )

    # Encryption / KMS
    system_tenant_id: str = "__system__"
    kms_provider: str = "local"  # local | vault | aws
    kms_local_keyfile: str = ".dev-master.key"
    kms_vault_url: str = "http://localhost:8200"
    kms_vault_token: str = ""
    kms_vault_transit_key: str = "p8-master"
    kms_aws_key_id: str = ""
    kms_aws_region: str = "us-east-1"
    dek_cache_ttl: int = 300

    # Agents
    default_model: str = "openai:gpt-4.1"  # fallback model when agent schema omits model_name
    schema_dir: str = "./schema"  # folder of YAML agent/schema definitions (may not exist)

    # Memory
    context_token_budget: int = 8000
    always_include_last_messages: int = 5
    moment_token_threshold: int = 6000  # build moment when session tokens exceed this
    moment_max_inject: int = 3          # inject last N moments into context

    # Debug — log raw LLM requests to console (OpenTelemetry console exporter)
    debug_llm: bool = False

    # S3 (optional, for FileService)
    s3_region: str = ""
    s3_endpoint_url: str = ""  # e.g. http://localhost:9000 for MinIO/localstack
    s3_bucket: str = ""  # default bucket for content uploads
    s3_access_key_id: str = ""      # explicit S3 credentials (Hetzner, MinIO)
    s3_secret_access_key: str = ""  # falls back to boto3 default credential chain

    # Worker (tiered QMS)
    worker_tier: str = "small"
    worker_poll_interval: float = 5.0
    worker_batch_size: int = 1

    # Content ingestion (Kreuzberg chunking)
    content_chunk_max_chars: int = 1500  # ~half a page of text
    content_chunk_overlap: int = 200

    # Audio processing
    audio_chunk_duration_ms: int = 30000  # 30s fallback chunk size
    audio_silence_thresh: int = -40       # dBFS
    audio_min_silence_len: int = 700      # ms

    # API key (simple bearer token for service-to-service auth)
    api_key: str = ""  # set P8_API_KEY to require Bearer token on all endpoints

    # Auth / JWT
    auth_secret_key: str = "changeme-in-production"
    auth_access_token_expiry: int = 3600       # 1h
    auth_refresh_token_expiry: int = 2592000   # 30d
    auth_magic_link_expiry: int = 600          # 10min

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""

    # Apple Sign In
    apple_client_id: str = ""
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key_path: str = ""

    # APNs (reuses apple_key_id / apple_team_id / apple_private_key_path above)
    apns_bundle_id: str = ""  # enables APNs when set (e.g. "com.yourapp.bundle")
    apns_environment: str = "production"  # "production" | "sandbox"

    # FCM v1 (Google Firebase Cloud Messaging)
    fcm_project_id: str = ""  # enables FCM when set
    fcm_service_account_file: str = ""  # path to Google service account JSON

    # Magic link email
    magic_link_base_url: str = ""              # defaults to api_base_url
    email_provider: str = "console"            # console | smtp | resend
    email_from: str = "noreply@p8.dev"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    resend_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""

    model_config = {"env_prefix": "P8_", "env_file": ".env", "extra": "ignore"}
