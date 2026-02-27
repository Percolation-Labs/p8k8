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
    embedding_min_similarity: float = 0.3  # default threshold for SEARCH; DB functions also default to 0.3
    embedding_batch_size: int = 20
    embedding_poll_interval: float = 2.0
    embedding_worker_enabled: bool = True  # False when pg_cron + pg_net handles scheduling

    # API (used by pg_cron pg_net to call back into the embedding processor)
    api_base_url: str = "http://localhost:8000"
    # Internal URL for pg_cron/pg_net HTTP calls (K8s service discovery).
    # Defaults to the K8s service name; override for local dev or non-K8s envs.
    internal_api_url: str = "http://p8-api.p8.svc:8000"

    # MCP server instructions (system prompt for MCP clients)
    mcp_instructions: str = (
        "Percolate is a personal memory and knowledge system. "
        "It stores the user's notes, conversations, web bookmarks, and structured metadata "
        "so you can help them recall, organize, and build on what they know.\n\n"
        "## Tools\n\n"
        "- **search**: Query the knowledge base using REM syntax — "
        "LOOKUP (exact key), SEARCH (semantic), FUZZY (text match), TRAVERSE (graph walk). "
        "Use this to find anything the user has saved or discussed before.\n"
        "- **get_moments**: Retrieve timestamped memories (session summaries, dreams, meetings) "
        "with filtering by type, category, tags, and date range. Great for recalling past conversations.\n"
        "- **web_search**: Search the web and optionally save results as resources for later recall.\n"
        "- **update_user_metadata**: Update the user's profile — relations, interests, feeds, preferences, facts. "
        "Use this when the user shares personal details worth remembering.\n"
        "- **remind_me**: Create one-time or recurring reminders with push notifications.\n"
        "- **get_user_profile**: Retrieve the current user's profile (name, email, metadata, tags). "
        "Call this early in a conversation to personalize responses.\n"
        "- **action**: Emit observation or elicit events for streaming UI updates.\n"
        "- **ask_agent**: Delegate to a specialized agent. "
        "Use sparingly — most tasks are better served by the tools above. "
        "Only delegate when a specific agent's expertise is needed (e.g. a domain-specific analyst).\n\n"
        "## Guidelines\n\n"
        "Prefer using search, get_moments, and update_user_metadata to help the user "
        "directly rather than delegating to agents. The memory tools give you everything "
        "you need to recall context, answer questions, and keep the user's profile current."
    )
    mcp_auth_enabled: bool = True  # set P8_MCP_AUTH_ENABLED=false to disable OAuth

    # Encryption / KMS
    system_tenant_id: str = "__system__"
    kms_provider: str = "local"  # local | vault | aws
    kms_local_keyfile: str = ".keys/.dev-master.key"
    kms_vault_url: str = "http://localhost:8200"
    kms_vault_token: str = ""
    kms_vault_transit_key: str = "p8-master"
    kms_aws_key_id: str = ""
    kms_aws_region: str = "us-east-1"
    dek_cache_ttl: int = 300

    # Agents
    default_model: str = "openai:gpt-4.1"  # fallback model when agent schema omits model_name
    default_temperature: float = 0.1
    default_max_tokens: int = 4000
    default_request_limit: int = 15
    default_token_limit: int = 80000
    # YAML agent/schema definitions folder. Set P8_SCHEMA_DIR to load agents from disk
    # e.g. P8_SCHEMA_DIR=.schema or P8_SCHEMA_DIR=/tmp/schema
    schema_dir: str = ""

    # Memory
    context_token_budget: int = 8000
    always_include_last_messages: int = 5
    moment_token_threshold: int = 6000  # build moment when session tokens exceed this
    moment_max_inject: int = 3          # inject last N moments into context

    # OpenTelemetry (disabled by default)
    otel_enabled: bool = False
    otel_service_name: str = "p8-api"
    otel_collector_endpoint: str = "http://localhost:4318"
    otel_protocol: str = "http"           # http | grpc
    otel_export_timeout: int = 10000      # ms
    otel_insecure: bool = True            # non-TLS for local dev

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
    file_processing_threshold_bytes: int = 5 * 1024 * 1024  # files above this queued to worker

    # Content ingestion (Kreuzberg chunking)
    content_chunk_max_chars: int = 1500  # ~half a page of text
    content_chunk_overlap: int = 200

    # Audio processing
    audio_chunk_duration_ms: int = 30000  # 30s fallback chunk size
    audio_silence_thresh: int = -40       # dBFS
    audio_min_silence_len: int = 700      # ms

    # API key (simple bearer token for service-to-service auth)
    api_key: str = ""  # set P8_API_KEY to require Bearer token on all endpoints
    master_key: str = ""    # P8_MASTER_KEY — full access, bypasses all filters
    tenant_keys: str = ""   # P8_TENANT_KEYS — JSON {"tenant_id": "key", ...}

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

    # Web search (Tavily)
    tavily_api_key: str = ""

    model_config = {"env_prefix": "P8_", "env_file": ".env", "extra": "ignore"}


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
