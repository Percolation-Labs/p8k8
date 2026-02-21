-- =============================================================================
-- install_entities.sql — Entity tables, table schema seeds, embeddings tables
--
-- Runs FIRST. Creates all entity tables that the core installer depends on.
-- Entity definitions can mutate across versions (via Alembic migrations)
-- without affecting the core install.
--
-- Table metadata lives in the schemas table with kind='table'. install.sql
-- reads these rows to dynamically apply triggers, indexes, and maintenance.
-- Adding a new entity table = CREATE TABLE + seed_table_schemas() entry.
--
-- Order: extensions → entity tables → seed function → embeddings tables →
--        privacy tables → schema_timemachine → seed call
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Extensions (needed before any table: UUID generation, vectors, trigrams)
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ---------------------------------------------------------------------------
-- Entity Tables
-- ---------------------------------------------------------------------------

-- schemas — the ontology registry (models, agents, evaluators, tools, AND tables)
CREATE TABLE IF NOT EXISTS schemas (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL UNIQUE,
    kind            VARCHAR(50) NOT NULL DEFAULT 'model',
    version         VARCHAR(50),
    description     TEXT,
    content         TEXT,
    json_schema     JSONB,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- ontologies — wiki pages, domain knowledge, parsed document entities
CREATE TABLE IF NOT EXISTS ontologies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    uri             TEXT,
    content         TEXT,
    extracted_data  JSONB,
    file_id         UUID,
    agent_schema_id UUID,
    confidence_score REAL,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- resources — documents, chunks, artifacts (ordered by ordinal)
CREATE TABLE IF NOT EXISTS resources (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    uri             TEXT,
    ordinal         INT,
    content         TEXT,
    category        VARCHAR(100),
    related_entities TEXT[],
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- moments — temporal events (session chunks, meetings, observations)
CREATE TABLE IF NOT EXISTS moments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    moment_type     VARCHAR(100),
    summary         TEXT,
    image_uri       TEXT,
    starts_timestamp TIMESTAMPTZ,
    ends_timestamp  TIMESTAMPTZ,
    present_persons JSONB DEFAULT '[]'::jsonb,
    emotion_tags    TEXT[] DEFAULT '{}',
    topic_tags      TEXT[] DEFAULT '{}',
    category        VARCHAR(100),
    source_session_id UUID,
    previous_moment_keys TEXT[] DEFAULT '{}',
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- sessions — conversation state
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255),
    description     TEXT,
    agent_name      VARCHAR(255),
    mode            VARCHAR(50),
    total_tokens    INT DEFAULT 0,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- messages — chat history
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID NOT NULL REFERENCES sessions(id),
    message_type    VARCHAR(20) NOT NULL DEFAULT 'user',
    content         TEXT,
    token_count     INT DEFAULT 0,
    tool_calls      JSONB,
    trace_id        VARCHAR(100),
    span_id         VARCHAR(100),
    input_tokens    INT DEFAULT 0,
    output_tokens   INT DEFAULT 0,
    latency_ms      INT,
    model           VARCHAR(100),
    agent_name      VARCHAR(255),
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- servers — remote tool server registry
CREATE TABLE IF NOT EXISTS servers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL UNIQUE,
    url             TEXT,
    protocol        VARCHAR(20) DEFAULT 'mcp',
    auth_config     JSONB DEFAULT '{}'::jsonb,
    enabled         BOOLEAN DEFAULT true,
    description     TEXT,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- tools — registered tool definitions
CREATE TABLE IF NOT EXISTS tools (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    server_id       UUID REFERENCES servers(id),
    description     TEXT,
    input_schema    JSONB,
    output_schema   JSONB,
    enabled         BOOLEAN DEFAULT true,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- users — user profiles
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    email           VARCHAR(255),
    interests       TEXT[] DEFAULT '{}',
    activity_level  VARCHAR(50),
    content         TEXT,
    devices         JSONB DEFAULT '[]'::jsonb,
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- files — uploaded/parsed documents
CREATE TABLE IF NOT EXISTS files (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    uri             TEXT,
    mime_type       VARCHAR(100),
    size_bytes      BIGINT,
    parsed_content  TEXT,
    parsed_output   JSONB,
    processing_status VARCHAR(20) DEFAULT 'pending',
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- Partial index for file processing queue (KEDA worker polls this)
CREATE INDEX IF NOT EXISTS idx_files_processing_status
    ON files(processing_status) WHERE processing_status = 'pending';

-- feedback — user ratings on agent responses
CREATE TABLE IF NOT EXISTS feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id      UUID,
    message_id      UUID,
    rating          INT,
    comment         TEXT,
    trace_id        VARCHAR(100),
    span_id         VARCHAR(100),
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- tenants — tenant entities (own users, encryption keys, scoped data)
CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(255) NOT NULL,
    encryption_mode VARCHAR(20) DEFAULT 'platform',
    status          VARCHAR(20) DEFAULT 'active',
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- storage_grants — cloud storage folder sync permissions
CREATE TABLE IF NOT EXISTS storage_grants (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id_ref     UUID NOT NULL REFERENCES users(id),
    provider        VARCHAR(50) NOT NULL,
    provider_folder_id VARCHAR(255),
    folder_name     VARCHAR(255),
    folder_path     TEXT,
    sync_mode       VARCHAR(20) DEFAULT 'incremental',
    auto_sync       BOOLEAN DEFAULT true,
    last_sync_at    TIMESTAMPTZ,
    sync_cursor     TEXT,
    status          VARCHAR(20) DEFAULT 'active',
    -- system fields
    tenant_id       VARCHAR(100),
    user_id         UUID,
    encryption_level VARCHAR(20),
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    metadata        JSONB DEFAULT '{}'::jsonb,
    tags            TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    deleted_at      TIMESTAMPTZ
);


-- ---------------------------------------------------------------------------
-- seed_table_schemas() — register entity tables in schemas with kind='table'
--
-- Each row's json_schema holds table metadata used by install.sql:
--   has_kv_sync     — sync to kv_store (requires 'name' column)
--   has_embeddings  — has companion embeddings_<table>
--   embedding_field — field to embed (NULL = no embeddings)
--   is_encrypted    — content may be ciphertext (KV uses name-only summary)
--   kv_summary_expr — SQL expression for KV content_summary
--
-- IDs are deterministic via uuid_generate_v5(P8_NAMESPACE, 'schemas:<name>:')
-- matching Python's deterministic_id() in ontology/base.py.
-- Idempotent — safe to call after TRUNCATE to re-seed.
-- ---------------------------------------------------------------------------

-- Shared namespace for deterministic UUID generation.
-- Must match P8_NAMESPACE in ontology/base.py:
--   uuid5(NAMESPACE_DNS, "p8.dev") = 'd122db5d-aceb-5673-b6e0-ce9e4328e725'
CREATE OR REPLACE FUNCTION p8_deterministic_id(
    p_table VARCHAR, p_key VARCHAR, p_user_id UUID DEFAULT NULL
) RETURNS UUID AS $$
BEGIN
    RETURN uuid_generate_v5(
        'd122db5d-aceb-5673-b6e0-ce9e4328e725'::uuid,
        p_table || ':' || p_key || ':' || COALESCE(p_user_id::text, '')
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;


CREATE OR REPLACE FUNCTION seed_table_schemas() RETURNS VOID AS $$
BEGIN
    INSERT INTO schemas (id, name, kind, description, json_schema) VALUES
    (p8_deterministic_id('schemas', 'schemas'),
     'schemas',        'table', 'Ontology registry — models, agents, evaluators, tools, tables',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "description",    "is_encrypted": false, "kv_summary_expr": "COALESCE(content, description, name)"}'::jsonb),

    (p8_deterministic_id('schemas', 'ontologies'),
     'ontologies',     'table', 'Wiki pages and domain knowledge entities',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "content",        "is_encrypted": true,  "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'resources'),
     'resources',      'table', 'Documents, chunks, and artifacts',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "content",        "is_encrypted": true,  "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'moments'),
     'moments',        'table', 'Temporal events — meetings, sessions, observations',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "summary",        "is_encrypted": true,  "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'sessions'),
     'sessions',       'table', 'Conversation state',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "description",    "is_encrypted": false, "kv_summary_expr": "COALESCE(description, name)"}'::jsonb),

    (p8_deterministic_id('schemas', 'messages'),
     'messages',       'table', 'Chat history messages',
     '{"has_kv_sync": false, "has_embeddings": true,  "embedding_field": "content",        "is_encrypted": true,  "kv_summary_expr": null}'::jsonb),

    (p8_deterministic_id('schemas', 'servers'),
     'servers',        'table', 'Remote tool server registry',
     '{"has_kv_sync": true,  "has_embeddings": false, "embedding_field": null,             "is_encrypted": false, "kv_summary_expr": "COALESCE(description, name)"}'::jsonb),

    (p8_deterministic_id('schemas', 'tools'),
     'tools',          'table', 'Registered tool definitions',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "description",    "is_encrypted": false, "kv_summary_expr": "COALESCE(description, name)"}'::jsonb),

    (p8_deterministic_id('schemas', 'users'),
     'users',          'table', 'User profiles',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "content",        "is_encrypted": true,  "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'files'),
     'files',          'table', 'Uploaded and parsed documents',
     '{"has_kv_sync": true,  "has_embeddings": true,  "embedding_field": "parsed_content", "is_encrypted": true,  "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'feedback'),
     'feedback',       'table', 'User ratings on agent responses',
     '{"has_kv_sync": false, "has_embeddings": false, "embedding_field": null,             "is_encrypted": true,  "kv_summary_expr": null}'::jsonb),

    (p8_deterministic_id('schemas', 'tenants'),
     'tenants',        'table', 'Tenant entities',
     '{"has_kv_sync": true,  "has_embeddings": false, "embedding_field": null,             "is_encrypted": false, "kv_summary_expr": "name"}'::jsonb),

    (p8_deterministic_id('schemas', 'storage_grants'),
     'storage_grants', 'table', 'Cloud storage folder sync permissions',
     '{"has_kv_sync": false, "has_embeddings": false, "embedding_field": null,             "is_encrypted": false, "kv_summary_expr": null}'::jsonb)

    ON CONFLICT (id) DO UPDATE SET
        name        = EXCLUDED.name,
        kind        = EXCLUDED.kind,
        description = EXCLUDED.description,
        json_schema = EXCLUDED.json_schema;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Companion Embeddings Tables
-- One per entity table where json_schema.has_embeddings = true.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS embeddings_schemas (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES schemas(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'description',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_ontologies (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES ontologies(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'content',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_resources (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'content',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_moments (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'summary',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_sessions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'description',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_messages (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'content',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_tools (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'description',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_users (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'content',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);

CREATE TABLE IF NOT EXISTS embeddings_files (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id   UUID NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'parsed_content',
    embedding   vector(1536) NOT NULL,
    provider    VARCHAR(50) DEFAULT 'openai',
    content_hash VARCHAR(64),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_id, field_name, provider)
);


-- ---------------------------------------------------------------------------
-- Privacy & Encryption Tables
-- ---------------------------------------------------------------------------

-- tenant_keys — per-tenant data encryption keys (wrapped by KMS master key)
CREATE TABLE IF NOT EXISTS tenant_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       VARCHAR(100) NOT NULL UNIQUE,
    wrapped_dek     BYTEA NOT NULL,
    kms_key_id      VARCHAR(255) NOT NULL,
    algorithm       VARCHAR(50) DEFAULT 'AES-256-GCM',
    status          VARCHAR(20) DEFAULT 'active',
    mode            VARCHAR(20) DEFAULT 'platform',  -- platform | client
    rotated_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- redaction_mappings — reversible PII redaction tokens
-- original_value is encrypted with the tenant DEK
CREATE TABLE IF NOT EXISTS redaction_mappings (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id       UUID NOT NULL,
    entity_table    VARCHAR(100) NOT NULL,
    session_id      UUID,
    token           VARCHAR(50) NOT NULL,
    original_value  TEXT NOT NULL,
    pii_type        VARCHAR(50) NOT NULL,
    tenant_id       VARCHAR(100),
    user_id         UUID,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


-- ---------------------------------------------------------------------------
-- Schema History (Time Machine)
-- Audit trail for schema changes. Trigger defined in install.sql.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_timemachine (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    schema_id   UUID NOT NULL,
    operation   VARCHAR(10) NOT NULL,
    name        VARCHAR(255),
    content     TEXT,
    json_schema JSONB,
    checksum    VARCHAR(64),
    recorded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


-- ---------------------------------------------------------------------------
-- Column migrations (safe to re-run — IF NOT EXISTS / IF EXISTS guards)
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN IF NOT EXISTS devices JSONB DEFAULT '[]'::jsonb;

-- Usage metrics on messages (for cost/latency aggregation per agent/model)
ALTER TABLE messages ADD COLUMN IF NOT EXISTS input_tokens INT DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS output_tokens INT DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS latency_ms INT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS model VARCHAR(100);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS agent_name VARCHAR(255);

-- Encryption level tracking (what mode was active when the row was written)
ALTER TABLE schemas ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE ontologies ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE resources ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE moments ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE servers ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE tools ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE files ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);
ALTER TABLE storage_grants ADD COLUMN IF NOT EXISTS encryption_level VARCHAR(20);


-- ---------------------------------------------------------------------------
-- Seed table registrations (runs after all tables exist)
-- ---------------------------------------------------------------------------

SELECT seed_table_schemas();
