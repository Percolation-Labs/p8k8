-- =============================================================================
-- 02_install.sql — Core infrastructure: functions, triggers, indexes, KV, queue
--
-- Runs AFTER 01_install_entities.sql. Depends on entity tables and
-- kind='table' schema rows existing (seeded by seed_table_schemas()).
--
-- All table iteration is driven by: schemas WHERE kind = 'table'
-- Adding a new entity table = CREATE TABLE + add to seed_table_schemas().
-- No changes needed in this file.
--
-- Order: extensions → UNLOGGED tables → helper functions →
--        REM functions → triggers → indexes → pg_cron jobs
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_cron;
-- pg_net: async HTTP from SQL — used by pg_cron to call the embedding API.
-- Optional: only needed when pg_cron drives embedding via HTTP (production).
-- The Python embedding worker handles this in dev/test mode.
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_net;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pg_net not available — embedding cron job will use Python worker instead';
END $$;

-- Grant app user access to pg_cron and pg_net schemas
-- (remind_me tool schedules jobs, embed-process uses pg_net)
DO $$ BEGIN
    GRANT USAGE ON SCHEMA cron TO p8user;
    GRANT USAGE ON SCHEMA net TO p8user;
    GRANT ALL ON ALL TABLES IN SCHEMA cron TO p8user;
    GRANT ALL ON ALL TABLES IN SCHEMA net TO p8user;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA cron TO p8user;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA net TO p8user;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Could not grant pg_cron/pg_net permissions: %', SQLERRM;
END $$;


-- ---------------------------------------------------------------------------
-- UNLOGGED Tables (fast writes, rebuilt on crash)
-- ---------------------------------------------------------------------------

-- KV store — O(1) entity resolution cache
CREATE UNLOGGED TABLE IF NOT EXISTS kv_store (
    entity_key      VARCHAR(255) NOT NULL,
    entity_type     VARCHAR(100) NOT NULL,
    entity_id       UUID NOT NULL,
    tenant_id       VARCHAR(100),
    user_id         UUID,
    content_summary TEXT,
    metadata        JSONB DEFAULT '{}'::jsonb,
    graph_edges     JSONB DEFAULT '[]'::jsonb,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
-- Functional unique constraint via index (can't inline COALESCE in CREATE TABLE)
CREATE UNIQUE INDEX IF NOT EXISTS idx_kv_store_tenant_key
    ON kv_store (COALESCE(tenant_id, ''), entity_key);

-- Embedding queue — async work queue for embedding generation
CREATE UNLOGGED TABLE IF NOT EXISTS embedding_queue (
    id          SERIAL PRIMARY KEY,
    table_name  VARCHAR(100) NOT NULL,
    entity_id   UUID NOT NULL,
    field_name  VARCHAR(100) NOT NULL DEFAULT 'content',
    provider    VARCHAR(50) DEFAULT NULL,
    status      VARCHAR(20) DEFAULT 'pending',
    attempts    INT DEFAULT 0,
    error       TEXT,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (table_name, entity_id, field_name)
);


-- ---------------------------------------------------------------------------
-- Helper Functions
-- ---------------------------------------------------------------------------

-- Normalize entity names to kebab-case keys for KV store resolution
CREATE OR REPLACE FUNCTION normalize_key(input TEXT) RETURNS TEXT AS $$
BEGIN
    RETURN lower(
        regexp_replace(
            regexp_replace(
                regexp_replace(trim(input), '[^a-zA-Z0-9\s\-_]', '', 'g'),
                '[\s_]+', '-', 'g'
            ),
            '-+', '-', 'g'
        )
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- Auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION update_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- KV store auto-population trigger
-- Applied to entity tables where json_schema.has_kv_sync = true.
-- Reads kv_summary_expr from the table's schema row for privacy-aware summaries.
CREATE OR REPLACE FUNCTION kv_store_upsert() RETURNS TRIGGER AS $$
DECLARE
    v_summary TEXT;
BEGIN
    IF TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL) THEN
        DELETE FROM kv_store
        WHERE entity_id = COALESCE(OLD.id, NEW.id)
          AND entity_type = TG_TABLE_NAME;
        RETURN COALESCE(OLD, NEW);
    END IF;

    -- Read kv_summary_expr from the table's schema registration.
    -- Falls back to 'name' if not found.
    BEGIN
        EXECUTE format(
            'SELECT LEFT((%s), 500) FROM (SELECT ($1).*) AS r',
            COALESCE(
                (SELECT s.json_schema->>'kv_summary_expr'
                 FROM schemas s
                 WHERE s.name = TG_TABLE_NAME AND s.kind = 'table'
                   AND s.deleted_at IS NULL),
                'name'
            )
        ) INTO v_summary USING NEW;
    EXCEPTION WHEN OTHERS THEN
        v_summary := LEFT(NEW.name, 500);
    END;

    INSERT INTO kv_store (
        entity_key, entity_type, entity_id,
        tenant_id, user_id, content_summary,
        metadata, graph_edges
    ) VALUES (
        normalize_key(NEW.name),
        TG_TABLE_NAME,
        NEW.id,
        NEW.tenant_id,
        NEW.user_id,
        v_summary,
        COALESCE(NEW.metadata, '{}'::jsonb),
        COALESCE(NEW.graph_edges, '[]'::jsonb)
    )
    ON CONFLICT (COALESCE(tenant_id, ''), entity_key)
    DO UPDATE SET
        content_summary = EXCLUDED.content_summary,
        metadata = EXCLUDED.metadata,
        graph_edges = EXCLUDED.graph_edges,
        updated_at = CURRENT_TIMESTAMP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- Embedding queue trigger
-- Enqueues embedding work when the embedded field changes.
-- TG_ARGV[0] is the field name to embed (e.g. 'content', 'description').
CREATE OR REPLACE FUNCTION queue_embedding() RETURNS TRIGGER AS $$
DECLARE
    v_field TEXT := TG_ARGV[0];
    v_old_val TEXT;
    v_new_val TEXT;
BEGIN
    -- Get old and new values dynamically
    EXECUTE format('SELECT ($1).%I', v_field) INTO v_new_val USING NEW;
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT ($1).%I', v_field) INTO v_old_val USING OLD;
    END IF;

    -- Only queue if the embedded field actually changed
    IF TG_OP = 'INSERT' OR v_new_val IS DISTINCT FROM v_old_val THEN
        INSERT INTO embedding_queue (table_name, entity_id, field_name, status)
        VALUES (TG_TABLE_NAME, NEW.id, v_field, 'pending')
        ON CONFLICT (table_name, entity_id, field_name)
        DO UPDATE SET status = 'pending', created_at = CURRENT_TIMESTAMP;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- Schema timemachine trigger
-- Records schema changes with SHA256 checksum for change detection.
CREATE OR REPLACE FUNCTION record_schema_timemachine() RETURNS TRIGGER AS $$
DECLARE
    v_checksum VARCHAR(64);
    v_row RECORD;
BEGIN
    v_row := COALESCE(NEW, OLD);
    v_checksum := encode(
        sha256(
            convert_to(COALESCE(v_row.content, '') || COALESCE(v_row.json_schema::text, ''), 'UTF8')
        ),
        'hex'
    );

    -- Skip no-op updates
    IF TG_OP = 'UPDATE' THEN
        DECLARE v_last_checksum VARCHAR(64);
        BEGIN
            SELECT checksum INTO v_last_checksum
            FROM schema_timemachine
            WHERE schema_id = v_row.id
            ORDER BY recorded_at DESC
            LIMIT 1;
            IF v_last_checksum = v_checksum THEN
                RETURN v_row;
            END IF;
        END;
    END IF;

    INSERT INTO schema_timemachine (schema_id, operation, name, content, json_schema, checksum)
    VALUES (v_row.id, TG_OP, v_row.name, v_row.content, v_row.json_schema, v_checksum);

    RETURN v_row;
END;
$$ LANGUAGE plpgsql;


-- Content extraction helper for embedding worker
CREATE OR REPLACE FUNCTION content_for_embedding(
    p_table VARCHAR,
    p_entity_id UUID,
    p_field VARCHAR DEFAULT 'content'
) RETURNS TEXT AS $$
DECLARE
    v_content TEXT;
BEGIN
    EXECUTE format(
        'SELECT %I FROM %I WHERE id = $1 AND deleted_at IS NULL',
        p_field, p_table
    ) INTO v_content USING p_entity_id;
    RETURN v_content;
END;
$$ LANGUAGE plpgsql;


-- Embedding upsert (called by Python worker after API response)
CREATE OR REPLACE FUNCTION upsert_embedding(
    p_table_name VARCHAR,
    p_entity_id UUID,
    p_field_name VARCHAR,
    p_embedding vector(1536),
    p_provider VARCHAR DEFAULT 'openai',
    p_content_hash VARCHAR DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    EXECUTE format(
        'INSERT INTO embeddings_%I (entity_id, field_name, embedding, provider, content_hash)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (entity_id, field_name, provider)
         DO UPDATE SET embedding = $3, content_hash = $5, created_at = CURRENT_TIMESTAMP',
        p_table_name
    ) USING p_entity_id, p_field_name, p_embedding, p_provider, p_content_hash;

    DELETE FROM embedding_queue
    WHERE table_name = p_table_name
      AND entity_id = p_entity_id
      AND field_name = p_field_name;
END;
$$ LANGUAGE plpgsql;


-- Embedding failure handler (retry up to 3 times, then mark failed)
CREATE OR REPLACE FUNCTION fail_embedding(
    p_table_name VARCHAR,
    p_entity_id UUID,
    p_field_name VARCHAR,
    p_error TEXT
) RETURNS VOID AS $$
BEGIN
    UPDATE embedding_queue
    SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
        error = p_error
    WHERE table_name = p_table_name
      AND entity_id = p_entity_id
      AND field_name = p_field_name;
END;
$$ LANGUAGE plpgsql;


-- Garbage collection for staged column drops (expand-contract pattern)
CREATE OR REPLACE FUNCTION gc_dropped_columns(
    p_retention_days INT DEFAULT 30
) RETURNS INT AS $$
DECLARE
    v_count INT := 0;
    v_col RECORD;
BEGIN
    FOR v_col IN
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE column_name LIKE '_dropped_%'
          AND table_schema = 'public'
    LOOP
        IF EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) -
           split_part(v_col.column_name, '_', array_length(string_to_array(v_col.column_name, '_'), 1))::bigint
           > p_retention_days * 86400 THEN
            EXECUTE format('ALTER TABLE %I DROP COLUMN %I',
                           v_col.table_name, v_col.column_name);
            v_count := v_count + 1;
        END IF;
    END LOOP;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- KV Store Maintenance Functions
-- ---------------------------------------------------------------------------

-- Full rebuild (crash recovery / manual reset)
-- Reads table config from schemas WHERE kind='table'.
CREATE OR REPLACE FUNCTION rebuild_kv_store() RETURNS VOID AS $$
DECLARE
    v_rec RECORD;
BEGIN
    TRUNCATE kv_store;
    FOR v_rec IN
        SELECT s.name AS table_name,
               COALESCE(s.json_schema->>'kv_summary_expr', 'name') AS summary_expr
        FROM schemas s
        WHERE s.kind = 'table'
          AND s.deleted_at IS NULL
          AND (s.json_schema->>'has_kv_sync')::boolean = true
    LOOP
        EXECUTE format(
            'INSERT INTO kv_store (entity_key, entity_type, entity_id,
                                   tenant_id, user_id, content_summary,
                                   metadata, graph_edges)
             SELECT normalize_key(name), %L, id,
                    tenant_id, user_id,
                    LEFT(%s, 500),
                    COALESCE(metadata, ''{}''::jsonb),
                    COALESCE(graph_edges, ''[]''::jsonb)
             FROM %I
             WHERE deleted_at IS NULL
             ON CONFLICT DO NOTHING',
            v_rec.table_name, v_rec.summary_expr, v_rec.table_name
        );
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- Incremental rebuild (hourly health check — only updates changed rows)
CREATE OR REPLACE FUNCTION rebuild_kv_store_incremental() RETURNS INT AS $$
DECLARE
    v_rec RECORD;
    v_count INT := 0;
    v_batch INT;
BEGIN
    FOR v_rec IN
        SELECT s.name AS table_name,
               COALESCE(s.json_schema->>'kv_summary_expr', 'name') AS summary_expr
        FROM schemas s
        WHERE s.kind = 'table'
          AND s.deleted_at IS NULL
          AND (s.json_schema->>'has_kv_sync')::boolean = true
    LOOP
        -- Upsert missing/stale entries (DISTINCT ON avoids duplicate normalized keys)
        EXECUTE format(
            'INSERT INTO kv_store (entity_key, entity_type, entity_id,
                                   tenant_id, user_id, content_summary,
                                   metadata, graph_edges)
             SELECT DISTINCT ON (COALESCE(tenant_id, ''''), normalize_key(name))
                    normalize_key(name), %L, id,
                    tenant_id, user_id,
                    LEFT(%s, 500),
                    COALESCE(metadata, ''{}''::jsonb),
                    COALESCE(graph_edges, ''[]''::jsonb)
             FROM %I
             WHERE deleted_at IS NULL
             ORDER BY COALESCE(tenant_id, ''''), normalize_key(name), updated_at DESC
             ON CONFLICT (COALESCE(tenant_id, ''''), entity_key)
             DO UPDATE SET
                content_summary = EXCLUDED.content_summary,
                metadata = EXCLUDED.metadata,
                graph_edges = EXCLUDED.graph_edges,
                updated_at = CURRENT_TIMESTAMP
             WHERE kv_store.content_summary IS DISTINCT FROM EXCLUDED.content_summary
                OR kv_store.metadata IS DISTINCT FROM EXCLUDED.metadata
                OR kv_store.graph_edges IS DISTINCT FROM EXCLUDED.graph_edges',
            v_rec.table_name, v_rec.summary_expr, v_rec.table_name
        );
        GET DIAGNOSTICS v_batch = ROW_COUNT;
        v_count := v_count + v_batch;

        -- Remove orphaned KV entries
        EXECUTE format(
            'DELETE FROM kv_store
             WHERE entity_type = %L
               AND entity_id NOT IN (SELECT id FROM %I WHERE deleted_at IS NULL)',
            v_rec.table_name, v_rec.table_name
        );
    END LOOP;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- REM Functions
-- ---------------------------------------------------------------------------

-- rem_lookup — O(1) entity lookup by normalized key → full entity row
CREATE OR REPLACE FUNCTION rem_lookup(
    p_entity_key VARCHAR(255),
    p_tenant_id VARCHAR(100) DEFAULT NULL,
    p_user_id UUID DEFAULT NULL
) RETURNS TABLE(entity_type VARCHAR, data JSONB) AS $$
DECLARE
    v_type  VARCHAR;
    v_id    UUID;
    v_data  JSONB;
BEGIN
    -- Step 1: O(1) lookup in kv_store index
    SELECT kv.entity_type, kv.entity_id
      INTO v_type, v_id
    FROM kv_store kv
    WHERE kv.entity_key = normalize_key(p_entity_key)
      AND (p_tenant_id IS NULL OR kv.tenant_id = p_tenant_id)
      AND (p_user_id IS NULL OR kv.user_id = p_user_id);

    IF v_type IS NULL THEN
        RETURN;
    END IF;

    -- Step 2: Dynamic join to source table for full entity data
    EXECUTE format(
        'SELECT row_to_json(t.*)::jsonb FROM %I t WHERE t.id = $1 AND t.deleted_at IS NULL',
        v_type
    ) INTO v_data USING v_id;

    IF v_data IS NOT NULL THEN
        RETURN QUERY SELECT v_type, v_data;
    END IF;
END;
$$ LANGUAGE plpgsql;


-- rem_search — semantic similarity search via pgvector
-- Drop old 8-param signature (without p_category) to avoid ambiguous overload
DROP FUNCTION IF EXISTS rem_search(vector, varchar, varchar, varchar, varchar, real, integer, uuid);
CREATE OR REPLACE FUNCTION rem_search(
    p_query_embedding vector,
    p_table_name VARCHAR(100),
    p_field_name VARCHAR(100) DEFAULT 'content',
    p_tenant_id VARCHAR(100) DEFAULT NULL,
    p_provider VARCHAR(50) DEFAULT 'openai',
    p_min_similarity REAL DEFAULT 0.3,
    p_limit INTEGER DEFAULT 10,
    p_user_id UUID DEFAULT NULL,
    p_category VARCHAR(100) DEFAULT NULL
) RETURNS TABLE(entity_type VARCHAR, similarity_score REAL, data JSONB) AS $$
DECLARE
    v_cat_filter TEXT := '';
    v_has_category BOOLEAN;
BEGIN
    -- Only filter on category if the target table has that column
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = p_table_name AND column_name = 'category'
    ) INTO v_has_category;

    IF v_has_category AND p_category IS NOT NULL THEN
        v_cat_filter := format(' AND t.category = %L', p_category);
    END IF;

    RETURN QUERY EXECUTE format(
        'SELECT %L::varchar AS entity_type,
                (1 - (e.embedding <=> $1))::real AS similarity_score,
                row_to_json(t.*)::jsonb AS data
         FROM embeddings_%I e
         JOIN %I t ON t.id = e.entity_id
         WHERE e.field_name = $2
           AND e.provider = $3
           AND (t.deleted_at IS NULL)
           AND ($4 IS NULL OR t.tenant_id = $4)
           AND ($5 IS NULL OR t.user_id = $5)
           AND (1 - (e.embedding <=> $1)) >= $6'
        || v_cat_filter ||
        ' ORDER BY e.embedding <=> $1
         LIMIT $7',
        p_table_name, p_table_name, p_table_name
    ) USING p_query_embedding, p_field_name, p_provider,
            p_tenant_id, p_user_id, p_min_similarity, p_limit;
END;
$$ LANGUAGE plpgsql;


-- rem_fuzzy — trigram text matching across KV store
CREATE OR REPLACE FUNCTION rem_fuzzy(
    p_query TEXT,
    p_tenant_id VARCHAR(100) DEFAULT NULL,
    p_threshold REAL DEFAULT 0.3,
    p_limit INTEGER DEFAULT 10,
    p_user_id UUID DEFAULT NULL
) RETURNS TABLE(entity_type VARCHAR, similarity_score REAL, data JSONB) AS $$
BEGIN
    RETURN QUERY
    SELECT kv.entity_type,
           GREATEST(
               similarity(kv.entity_key, p_query),
               similarity(kv.content_summary, p_query)
           )::real AS similarity_score,
           jsonb_build_object(
               'id', kv.entity_id,
               'key', kv.entity_key,
               'type', kv.entity_type,
               'summary', kv.content_summary,
               'metadata', kv.metadata,
               'graph_edges', kv.graph_edges
           ) AS data
    FROM kv_store kv
    WHERE (p_tenant_id IS NULL OR kv.tenant_id = p_tenant_id)
      AND (p_user_id IS NULL OR kv.user_id = p_user_id)
      AND GREATEST(
              similarity(kv.entity_key, p_query),
              similarity(kv.content_summary, p_query)
          ) >= p_threshold
    ORDER BY similarity_score DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql;


-- rem_traverse — recursive graph walk via graph_edges JSONB
--
-- Three modes controlled by p_keys_only and p_load:
--   default (both false)  → lazy: keys + summary/metadata from kv_store
--   p_keys_only = true    → keys only: no entity_record at all
--   p_load = true         → load: full entity rows from source tables (like LOOKUP)
--
-- Prefer lazy mode for agents exploring the graph — LOOKUP specific nodes after.
DROP FUNCTION IF EXISTS rem_traverse(VARCHAR, VARCHAR, UUID, INTEGER, VARCHAR, BOOLEAN);

CREATE OR REPLACE FUNCTION rem_traverse(
    p_entity_key VARCHAR(255),
    p_tenant_id VARCHAR(100) DEFAULT NULL,
    p_user_id UUID DEFAULT NULL,
    p_max_depth INTEGER DEFAULT 1,
    p_rel_type VARCHAR(100) DEFAULT NULL,
    p_keys_only BOOLEAN DEFAULT FALSE,
    p_load BOOLEAN DEFAULT FALSE
) RETURNS TABLE(
    depth INT, entity_key VARCHAR, entity_type VARCHAR,
    entity_id UUID, rel_type VARCHAR, rel_weight REAL,
    path TEXT[], entity_record JSONB
) AS $$
DECLARE
    v_row RECORD;
    v_data JSONB;
BEGIN
    -- Run the recursive CTE and iterate results
    FOR v_row IN
        WITH RECURSIVE traversal AS (
            -- Seed: starting node
            SELECT 0 AS depth,
                   kv.entity_key::varchar AS entity_key,
                   kv.entity_type::varchar AS entity_type,
                   kv.entity_id,
                   NULL::varchar AS rel_type, 1.0::real AS rel_weight,
                   ARRAY[kv.entity_key::text] AS path,
                   jsonb_build_object('summary', kv.content_summary,
                                      'metadata', kv.metadata) AS kv_record
            FROM kv_store kv
            WHERE kv.entity_key = normalize_key(p_entity_key)
              AND (p_tenant_id IS NULL OR kv.tenant_id = p_tenant_id)
              AND (p_user_id IS NULL OR kv.user_id = p_user_id)

            UNION ALL

            -- Walk edges
            SELECT t.depth + 1,
                   normalize_key(edge->>'target')::varchar,
                   kv2.entity_type::varchar,
                   kv2.entity_id,
                   (edge->>'relation')::varchar,
                   COALESCE((edge->>'weight')::real, 1.0),
                   t.path || normalize_key(edge->>'target')::text,
                   jsonb_build_object('summary', kv2.content_summary,
                                      'metadata', kv2.metadata)
            FROM traversal t
            JOIN kv_store kv_src ON kv_src.entity_key = t.entity_key
            CROSS JOIN LATERAL jsonb_array_elements(kv_src.graph_edges) AS edge
            JOIN kv_store kv2 ON kv2.entity_key = normalize_key(edge->>'target')
            WHERE t.depth < p_max_depth
              AND NOT (normalize_key(edge->>'target') = ANY(t.path))
              AND (p_rel_type IS NULL OR edge->>'relation' = p_rel_type)
              AND (p_tenant_id IS NULL OR kv2.tenant_id = p_tenant_id)
              AND (p_user_id IS NULL OR kv2.user_id = p_user_id)
        )
        SELECT * FROM traversal
    LOOP
        -- Determine entity_record based on mode
        IF p_keys_only THEN
            v_data := NULL;
        ELSIF p_load THEN
            -- Dynamic join to source table for full entity data (like LOOKUP)
            EXECUTE format(
                'SELECT row_to_json(t.*)::jsonb FROM %I t WHERE t.id = $1 AND t.deleted_at IS NULL',
                v_row.entity_type
            ) INTO v_data USING v_row.entity_id;
        ELSE
            -- Lazy: summary + metadata from kv_store
            v_data := v_row.kv_record;
        END IF;

        RETURN QUERY SELECT v_row.depth, v_row.entity_key, v_row.entity_type,
                            v_row.entity_id, v_row.rel_type, v_row.rel_weight,
                            v_row.path, v_data;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- rem_load_messages — flexible message loader with optional constraints
-- All filter params are optional (NULL = no limit):
--   p_max_tokens  — cumulative token budget (most-recent first)
--   p_max_messages — maximum message count
--   p_since       — only messages after this timestamp
-- Returns messages in chronological order (oldest first).
CREATE OR REPLACE FUNCTION rem_load_messages(
    p_session_id UUID,
    p_max_tokens INT DEFAULT NULL,
    p_max_messages INT DEFAULT NULL,
    p_since TIMESTAMPTZ DEFAULT NULL
) RETURNS TABLE(
    id UUID, message_type VARCHAR, content TEXT,
    token_count INT, tool_calls JSONB,
    created_at TIMESTAMPTZ, running_tokens BIGINT
) AS $$
WITH filtered AS (
    -- All messages for this session, optionally filtered by date
    SELECT m.id, m.message_type, m.content,
           m.token_count, m.tool_calls, m.created_at
    FROM messages m
    WHERE m.session_id = p_session_id
      AND m.deleted_at IS NULL
      AND (p_since IS NULL OR m.created_at >= p_since)
),
numbered AS (
    -- Number from most recent → oldest for budget/count limits
    SELECT f.*,
           ROW_NUMBER() OVER (ORDER BY f.created_at DESC) AS rn
    FROM filtered f
),
with_running AS (
    -- Cumulative token sum from most recent backwards
    SELECT n.*,
           SUM(n.token_count) OVER (ORDER BY n.rn) AS running_tokens
    FROM numbered n
)
SELECT w.id, w.message_type, w.content,
       w.token_count, w.tool_calls, w.created_at,
       w.running_tokens
FROM with_running w
WHERE (p_max_messages IS NULL OR w.rn <= p_max_messages)
  AND (p_max_tokens   IS NULL OR w.running_tokens <= p_max_tokens)
ORDER BY w.created_at ASC;
$$ LANGUAGE sql;


-- Drop old signatures if return type changed (safe — CREATE OR REPLACE follows)
DROP FUNCTION IF EXISTS rem_build_moment(UUID, VARCHAR, UUID, INT);
DROP FUNCTION IF EXISTS rem_persist_turn(UUID, TEXT, TEXT, UUID, VARCHAR, JSONB, JSONB, INT);
DROP FUNCTION IF EXISTS rem_persist_turn(UUID, TEXT, TEXT, UUID, VARCHAR, JSONB, JSONB, INT, INT, INT, INT, VARCHAR, VARCHAR);

-- rem_build_moment — atomically build a session_chunk moment from messages
-- since the last moment.  Optionally checks a token threshold first (set
-- p_threshold = 0 to skip the check and always build).
--
-- Returns the new moment row, or no rows if below threshold / no messages.
-- In a single round-trip this function:
--   1. Finds the last session_chunk moment for the session
--   2. Sums tokens already covered by prior moments
--   3. Compares tokens-since-last-moment against threshold
--   4. Fetches messages since last moment
--   5. Builds summary from assistant message content
--   6. Generates a deterministic moment name
--   7. INSERTs the moment (ON CONFLICT update)
--   8. UPDATEs session metadata with latest moment context
CREATE OR REPLACE FUNCTION rem_build_moment(
    p_session_id  UUID,
    p_tenant_id   VARCHAR DEFAULT NULL,
    p_user_id     UUID    DEFAULT NULL,
    p_threshold   INT     DEFAULT 0
) RETURNS TABLE(
    moment_id         UUID,
    moment_name       VARCHAR,
    moment_type       VARCHAR,
    summary           TEXT,
    chunk_index       INT,
    message_count     INT,
    token_count       INT,
    source_session_id UUID,
    starts_timestamp  TIMESTAMPTZ,
    ends_timestamp    TIMESTAMPTZ,
    previous_keys     TEXT[]
) AS $$
DECLARE
    -- P8_NAMESPACE = uuid5(NAMESPACE_DNS, 'p8.dev')
    -- Must match ontology/base.py P8_NAMESPACE for deterministic IDs
    c_namespace  CONSTANT UUID := 'd122db5d-aceb-5673-b6e0-ce9e4328e725';
    v_last_moment    RECORD;
    v_has_prior      BOOLEAN := FALSE;
    v_chunk_index    INT := 0;
    v_prev_keys      TEXT[] := '{}';
    v_tokens_covered BIGINT := 0;
    v_session_tokens INT;
    v_msg_count      INT;
    v_token_sum      BIGINT;
    v_first_ts       TIMESTAMPTZ;
    v_last_ts        TIMESTAMPTZ;
    v_summary        TEXT;
    v_short_hash     TEXT;
    v_date_str       TEXT;
    v_name           VARCHAR;
    v_moment_id      UUID;
BEGIN
    -- 1. Find last session_chunk moment
    --    NOTE: use FOUND (not IS NOT NULL) because RECORD IS NOT NULL
    --    requires ALL fields non-null — nullable columns like deleted_at break it.
    SELECT m.* INTO v_last_moment
    FROM moments m
    WHERE m.source_session_id = p_session_id
      AND m.moment_type = 'session_chunk'
      AND m.deleted_at IS NULL
    ORDER BY m.created_at DESC LIMIT 1;

    v_has_prior := FOUND;
    IF v_has_prior THEN
        v_chunk_index := COALESCE((v_last_moment.metadata->>'chunk_index')::int, 0) + 1;
        v_prev_keys := ARRAY[v_last_moment.name];
    END IF;

    -- 2. Threshold check (skip if p_threshold = 0)
    IF p_threshold > 0 THEN
        SELECT COALESCE(s.total_tokens, 0) INTO v_session_tokens
        FROM sessions s WHERE s.id = p_session_id AND s.deleted_at IS NULL;

        IF v_session_tokens IS NULL THEN RETURN; END IF;

        -- Sum tokens covered by all prior session_chunk moments
        SELECT COALESCE(SUM((mo.metadata->>'token_count')::int), 0) INTO v_tokens_covered
        FROM moments mo
        WHERE mo.source_session_id = p_session_id
          AND mo.moment_type = 'session_chunk'
          AND mo.deleted_at IS NULL;

        IF (v_session_tokens - v_tokens_covered) < p_threshold THEN
            RETURN;  -- below threshold, return empty
        END IF;
    END IF;

    -- 3. Aggregate messages since last moment
    IF v_has_prior THEN
        SELECT COUNT(*), COALESCE(SUM(m.token_count), 0),
               MIN(m.created_at), MAX(m.created_at)
        INTO v_msg_count, v_token_sum, v_first_ts, v_last_ts
        FROM messages m
        WHERE m.session_id = p_session_id
          AND m.created_at > v_last_moment.created_at
          AND m.deleted_at IS NULL;
    ELSE
        SELECT COUNT(*), COALESCE(SUM(m.token_count), 0),
               MIN(m.created_at), MAX(m.created_at)
        INTO v_msg_count, v_token_sum, v_first_ts, v_last_ts
        FROM messages m
        WHERE m.session_id = p_session_id
          AND m.deleted_at IS NULL;
    END IF;

    IF v_msg_count = 0 THEN RETURN; END IF;

    -- 4. Build summary from assistant messages (truncated to 2000 chars)
    IF v_has_prior THEN
        SELECT LEFT(string_agg(m.content, E'\n' ORDER BY m.created_at), 2000)
        INTO v_summary
        FROM messages m
        WHERE m.session_id = p_session_id
          AND m.message_type = 'assistant'
          AND m.content IS NOT NULL
          AND m.created_at > v_last_moment.created_at
          AND m.deleted_at IS NULL;
    ELSE
        SELECT LEFT(string_agg(m.content, E'\n' ORDER BY m.created_at), 2000)
        INTO v_summary
        FROM messages m
        WHERE m.session_id = p_session_id
          AND m.message_type = 'assistant'
          AND m.content IS NOT NULL
          AND m.deleted_at IS NULL;
    END IF;

    -- 5. Generate deterministic name
    v_short_hash := LEFT(encode(digest(p_session_id::text, 'sha256'), 'hex'), 6);
    v_date_str := to_char(COALESCE(v_first_ts, NOW()), 'YYYYMMDD');
    v_name := 'session-' || v_short_hash || '-' || v_date_str || '-chunk-' || v_chunk_index;

    -- 6. Deterministic ID (must match ontology/base.py deterministic_id('moments', name))
    v_moment_id := uuid_generate_v5(c_namespace, 'moments:' || v_name || ':');

    -- 7. Upsert moment
    INSERT INTO moments (id, name, moment_type, summary, source_session_id,
                         starts_timestamp, ends_timestamp, previous_moment_keys,
                         tenant_id, user_id, metadata)
    VALUES (v_moment_id, v_name, 'session_chunk', v_summary, p_session_id,
            v_first_ts, v_last_ts, v_prev_keys,
            p_tenant_id, p_user_id,
            jsonb_build_object(
                'message_count', v_msg_count,
                'token_count', v_token_sum,
                'chunk_index', v_chunk_index
            ))
    ON CONFLICT (id) DO UPDATE SET
        summary = EXCLUDED.summary,
        starts_timestamp = EXCLUDED.starts_timestamp,
        ends_timestamp = EXCLUDED.ends_timestamp,
        previous_moment_keys = EXCLUDED.previous_moment_keys,
        metadata = EXCLUDED.metadata,
        updated_at = CURRENT_TIMESTAMP;

    -- 8. Update session metadata
    UPDATE sessions SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
        'latest_moment_id', v_moment_id::text,
        'latest_summary', LEFT(v_summary, 200),
        'moment_count', v_chunk_index + 1
    ) WHERE id = p_session_id;

    -- 9. Return the built moment
    RETURN QUERY SELECT
        v_moment_id,
        v_name::varchar,
        'session_chunk'::varchar,
        v_summary,
        v_chunk_index,
        v_msg_count,
        v_token_sum::int,
        p_session_id,
        v_first_ts,
        v_last_ts,
        v_prev_keys;
END;
$$ LANGUAGE plpgsql;


-- Drop old overload that included p_pai_messages parameter
DROP FUNCTION IF EXISTS rem_persist_turn(UUID, TEXT, TEXT, UUID, VARCHAR, JSONB, JSONB, INT, INT, INT, INT, VARCHAR, VARCHAR, VARCHAR, UUID, UUID);

-- rem_persist_turn — atomically persist a user+assistant message pair,
-- update session token totals, and optionally trigger moment building
-- if threshold exceeded.
--
-- Batches 2 INSERTs + 1 UPDATE + optional moment build into one round-trip.
-- Returns the user message ID, assistant message ID, and optional moment name.
CREATE OR REPLACE FUNCTION rem_persist_turn(
    p_session_id       UUID,
    p_user_content     TEXT,
    p_assistant_content TEXT,
    p_user_id          UUID    DEFAULT NULL,
    p_tenant_id        VARCHAR DEFAULT NULL,
    p_tool_calls       JSONB   DEFAULT NULL,
    p_moment_threshold INT     DEFAULT 0,
    p_input_tokens     INT     DEFAULT 0,
    p_output_tokens    INT     DEFAULT 0,
    p_latency_ms       INT     DEFAULT NULL,
    p_model            VARCHAR DEFAULT NULL,
    p_agent_name       VARCHAR DEFAULT NULL,
    p_encryption_level VARCHAR DEFAULT NULL,
    p_user_msg_id      UUID    DEFAULT NULL,
    p_asst_msg_id      UUID    DEFAULT NULL
) RETURNS TABLE(
    user_message_id      UUID,
    assistant_message_id UUID,
    user_tokens          INT,
    assistant_tokens     INT,
    moment_name          VARCHAR
) AS $$
DECLARE
    v_user_msg_id UUID;
    v_asst_msg_id UUID;
    v_user_tokens INT;
    v_asst_tokens INT;
    v_moment_name VARCHAR;
    v_moment_row RECORD;
BEGIN
    -- Estimate token counts (~4 chars per token)
    v_user_tokens := GREATEST(COALESCE(LENGTH(p_user_content) / 4, 0), 0);
    v_asst_tokens := GREATEST(COALESCE(LENGTH(p_assistant_content) / 4, 0), 0);

    -- Use pre-generated IDs if provided (needed for encryption AAD binding)
    v_user_msg_id := COALESCE(p_user_msg_id, gen_random_uuid());
    v_asst_msg_id := COALESCE(p_asst_msg_id, gen_random_uuid());

    -- 1. Insert user message
    INSERT INTO messages (id, session_id, message_type, content, token_count, tenant_id, user_id, encryption_level)
    VALUES (v_user_msg_id, p_session_id, 'user', p_user_content, v_user_tokens, p_tenant_id, p_user_id, p_encryption_level);

    -- 2. Insert assistant message (with usage metrics)
    INSERT INTO messages (id, session_id, message_type, content, token_count, tool_calls,
                          input_tokens, output_tokens, latency_ms, model, agent_name,
                          tenant_id, user_id, encryption_level)
    VALUES (v_asst_msg_id, p_session_id, 'assistant', p_assistant_content, v_asst_tokens, p_tool_calls,
            p_input_tokens, p_output_tokens, p_latency_ms, p_model, p_agent_name,
            p_tenant_id, p_user_id, p_encryption_level);

    -- 3. Update session token total
    UPDATE sessions SET total_tokens = total_tokens + v_user_tokens + v_asst_tokens
    WHERE id = p_session_id;

    -- 4. Optionally build moment if threshold > 0
    v_moment_name := NULL;
    IF p_moment_threshold > 0 THEN
        SELECT bm.moment_name INTO v_moment_name
        FROM rem_build_moment(p_session_id, p_tenant_id, p_user_id, p_moment_threshold) bm;
    END IF;

    RETURN QUERY SELECT v_user_msg_id, v_asst_msg_id, v_user_tokens, v_asst_tokens, v_moment_name;
END;
$$ LANGUAGE plpgsql;


-- clone_session — deep-copy a session with its messages for LLM testing
-- Copies the session row with a new UUID, then copies messages (in order).
-- Optional: limit message count, override user_id/agent_name on the clone.
CREATE OR REPLACE FUNCTION clone_session(
    p_source_session_id UUID,
    p_max_messages INT DEFAULT NULL,
    p_new_user_id UUID DEFAULT NULL,
    p_new_agent_name VARCHAR(255) DEFAULT NULL
) RETURNS TABLE(new_session_id UUID, messages_copied INT) AS $$
DECLARE
    v_new_id UUID := uuid_generate_v4();
    v_count INT;
BEGIN
    -- 1. Clone session row
    INSERT INTO sessions (id, name, description, agent_name, mode, total_tokens,
                          tenant_id, user_id, graph_edges, metadata, tags)
    SELECT v_new_id,
           s.name || ' (clone)',
           s.description,
           COALESCE(p_new_agent_name, s.agent_name),
           s.mode,
           0,
           s.tenant_id,
           COALESCE(p_new_user_id, s.user_id),
           s.graph_edges,
           s.metadata || jsonb_build_object('cloned_from', p_source_session_id::text),
           s.tags
    FROM sessions s
    WHERE s.id = p_source_session_id AND s.deleted_at IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'session % not found', p_source_session_id;
    END IF;

    -- 2. Clone messages (chronological order, optional limit)
    WITH ranked AS (
        SELECT m.*,
               ROW_NUMBER() OVER (ORDER BY m.created_at ASC) AS rn
        FROM messages m
        WHERE m.session_id = p_source_session_id
          AND m.deleted_at IS NULL
    )
    INSERT INTO messages (id, session_id, message_type, content, token_count,
                          tool_calls, trace_id, span_id,
                          input_tokens, output_tokens, latency_ms, model, agent_name,
                          tenant_id, user_id, graph_edges, metadata, tags)
    SELECT uuid_generate_v4(),
           v_new_id,
           r.message_type,
           r.content,
           r.token_count,
           r.tool_calls,
           r.trace_id,
           r.span_id,
           r.input_tokens,
           r.output_tokens,
           r.latency_ms,
           r.model,
           r.agent_name,
           r.tenant_id,
           COALESCE(p_new_user_id, r.user_id),
           r.graph_edges,
           r.metadata,
           r.tags
    FROM ranked r
    WHERE p_max_messages IS NULL OR r.rn <= p_max_messages;

    GET DIAGNOSTICS v_count = ROW_COUNT;

    -- 3. Update token total on clone
    UPDATE sessions SET total_tokens = (
        SELECT COALESCE(SUM(token_count), 0) FROM messages WHERE session_id = v_new_id
    ) WHERE id = v_new_id;

    RETURN QUERY SELECT v_new_id, v_count;
END;
$$ LANGUAGE plpgsql;


-- search_sessions — paginated session search with multi-modal filters
-- All filter params optional (NULL = skip). Pagination via CTE.
-- When p_query_embedding is provided, results ranked by semantic similarity.
-- Otherwise ranked by created_at DESC.
CREATE OR REPLACE FUNCTION search_sessions(
    p_query TEXT DEFAULT NULL,                   -- ILIKE on session name
    p_user_id UUID DEFAULT NULL,
    p_agent_name VARCHAR(255) DEFAULT NULL,
    p_tags TEXT[] DEFAULT NULL,                   -- tags @> containment
    p_tenant_id VARCHAR(100) DEFAULT NULL,
    p_since TIMESTAMPTZ DEFAULT NULL,
    p_query_embedding vector DEFAULT NULL,        -- semantic on description
    p_min_similarity REAL DEFAULT 0.3,
    p_page INT DEFAULT 1,
    p_page_size INT DEFAULT 20
) RETURNS TABLE(
    id UUID, name VARCHAR, description TEXT,
    agent_name VARCHAR, mode VARCHAR, total_tokens INT,
    message_count BIGINT,
    user_id UUID, tenant_id VARCHAR,
    tags TEXT[], metadata JSONB, created_at TIMESTAMPTZ,
    similarity REAL, total_results BIGINT
) AS $$
WITH filtered AS (
    SELECT s.id, s.name, s.description, s.agent_name, s.mode,
           s.total_tokens, s.user_id, s.tenant_id, s.tags, s.metadata,
           s.created_at,
           CASE WHEN p_query_embedding IS NOT NULL AND e.embedding IS NOT NULL
                THEN 1 - (e.embedding <=> p_query_embedding)
                ELSE NULL
           END AS similarity
    FROM sessions s
    LEFT JOIN embeddings_sessions e
        ON e.entity_id = s.id
        AND e.field_name = 'description'
        AND p_query_embedding IS NOT NULL
    WHERE s.deleted_at IS NULL
      AND (p_query IS NULL      OR s.name ILIKE '%' || p_query || '%')
      AND (p_user_id IS NULL    OR s.user_id = p_user_id)
      AND (p_agent_name IS NULL OR s.agent_name = p_agent_name)
      AND (p_tags IS NULL       OR s.tags @> p_tags)
      AND (p_tenant_id IS NULL  OR s.tenant_id = p_tenant_id)
      AND (p_since IS NULL      OR s.created_at >= p_since)
      AND (p_query_embedding IS NULL OR (
           e.embedding IS NOT NULL
           AND 1 - (e.embedding <=> p_query_embedding) >= p_min_similarity
      ))
),
counted AS (
    SELECT COUNT(*) AS total FROM filtered
),
paged AS (
    SELECT f.*,
           (SELECT COUNT(*) FROM messages m
            WHERE m.session_id = f.id AND m.deleted_at IS NULL) AS message_count
    FROM filtered f
    ORDER BY
        CASE WHEN p_query_embedding IS NOT NULL THEN f.similarity END DESC NULLS LAST,
        f.created_at DESC
    LIMIT p_page_size OFFSET (p_page - 1) * p_page_size
)
SELECT p.id, p.name, p.description, p.agent_name, p.mode,
       p.total_tokens, p.message_count,
       p.user_id, p.tenant_id, p.tags, p.metadata, p.created_at,
       COALESCE(p.similarity, 0.0)::REAL,
       c.total
FROM paged p, counted c;
$$ LANGUAGE sql;


-- rem_session_timeline — interleaved messages + moments for a session
-- DROP first because return-type changes are not allowed by CREATE OR REPLACE
DROP FUNCTION IF EXISTS rem_session_timeline(UUID, INT);
CREATE OR REPLACE FUNCTION rem_session_timeline(
    p_session_id UUID,
    p_limit INT DEFAULT 50
) RETURNS TABLE(
    event_type VARCHAR,
    event_id UUID,
    event_timestamp TIMESTAMPTZ,
    name_or_type VARCHAR,
    content_or_summary TEXT,
    encryption_level VARCHAR,
    metadata JSONB
) AS $$
    SELECT 'message'::varchar, m.id, m.created_at, m.message_type::varchar,
           m.content, m.encryption_level,
           jsonb_build_object('token_count', m.token_count, 'tool_calls', m.tool_calls)
    FROM messages m
    WHERE m.session_id = p_session_id AND m.deleted_at IS NULL
    UNION ALL
    SELECT 'moment'::varchar, mo.id, COALESCE(mo.starts_timestamp, mo.created_at),
           COALESCE(mo.moment_type, 'unknown')::varchar, mo.summary, mo.encryption_level,
           jsonb_build_object('name', mo.name, 'ends_timestamp', mo.ends_timestamp,
                              'previous_moment_keys', mo.previous_moment_keys,
                              'moment_metadata', mo.metadata)
    FROM moments mo
    WHERE mo.source_session_id = p_session_id AND mo.deleted_at IS NULL
    ORDER BY 3 ASC
    LIMIT p_limit;
$$ LANGUAGE sql;


-- rem_moments_feed — cursor-paginated feed of real moments + virtual daily summary cards.
--
-- Pagination is cursor-based: p_before_date bounds all CTEs so they only scan
-- the requested date window (p_limit active dates starting before the cursor).
-- The client passes the oldest event_date from the previous page as the next
-- cursor.  First request: p_before_date = NULL (starts from today).
--
-- For each date with activity, a daily_summary row is synthesized with stats
-- (message count, tokens, session count, moment count) and a deterministic
-- session UUID derived from (user_id, date) so the client can chat with that day.
DROP FUNCTION IF EXISTS rem_moments_feed(UUID, INT, DATE);
DROP FUNCTION IF EXISTS rem_moments_feed(UUID, INT, DATE, BOOLEAN);
CREATE OR REPLACE FUNCTION rem_moments_feed(
    p_user_id        UUID    DEFAULT NULL,
    p_limit          INT     DEFAULT 20,
    p_before_date    DATE    DEFAULT NULL,
    p_include_future BOOLEAN DEFAULT FALSE
) RETURNS TABLE(
    event_type       VARCHAR,
    event_id         UUID,
    event_date       DATE,
    event_timestamp  TIMESTAMPTZ,
    name             VARCHAR,
    moment_type      VARCHAR,
    summary          TEXT,
    session_id       UUID,
    image            TEXT,
    encryption_level VARCHAR,
    metadata         JSONB
) AS $$
WITH
-- 1. Find the next p_limit active dates before the cursor.
--    This bounds all downstream CTEs to a small date window.
--    Includes dates from both messages AND moments so upload-only days appear.
active_dates AS (
    SELECT DISTINCT d FROM (
        SELECT (m.created_at AT TIME ZONE 'UTC')::date AS d
        FROM messages m
        WHERE m.deleted_at IS NULL
          AND (p_user_id IS NULL OR m.user_id = p_user_id)
          AND (p_before_date IS NULL OR (m.created_at AT TIME ZONE 'UTC')::date <= p_before_date)
        UNION
        SELECT (mo.created_at AT TIME ZONE 'UTC')::date AS d
        FROM moments mo
        WHERE mo.deleted_at IS NULL
          AND (p_user_id IS NULL OR mo.user_id = p_user_id)
          AND (p_before_date IS NULL OR (mo.created_at AT TIME ZONE 'UTC')::date <= p_before_date)
        UNION
        SELECT (r.created_at AT TIME ZONE 'UTC')::date AS d
        FROM resources r
        WHERE r.deleted_at IS NULL
          AND r.category IS NOT NULL
          AND (p_user_id IS NULL OR r.user_id = p_user_id)
          AND (p_before_date IS NULL OR (r.created_at AT TIME ZONE 'UTC')::date <= p_before_date)
    ) AS all_dates
    ORDER BY d DESC
    LIMIT p_limit
),

-- 2. Per-date stats — only for dates in the window
daily_stats AS (
    SELECT
        (m.created_at AT TIME ZONE 'UTC')::date AS d,
        COUNT(*)                                 AS msg_count,
        COALESCE(SUM(m.token_count), 0)          AS total_tokens,
        COUNT(DISTINCT m.session_id)             AS session_count
    FROM messages m
    WHERE m.deleted_at IS NULL
      AND (p_user_id IS NULL OR m.user_id = p_user_id)
      AND (m.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
    GROUP BY 1
),

daily_moment_counts AS (
    SELECT
        (mo.created_at AT TIME ZONE 'UTC')::date AS d,
        COUNT(*) AS moment_count
    FROM moments mo
    WHERE mo.deleted_at IS NULL
      AND (p_user_id IS NULL OR mo.user_id = p_user_id)
      AND (mo.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
    GROUP BY 1
),

daily_reminder_counts AS (
    SELECT
        (mo.created_at AT TIME ZONE 'UTC')::date AS d,
        COUNT(*) AS reminder_count
    FROM moments mo
    WHERE mo.deleted_at IS NULL
      AND mo.moment_type = 'reminder'
      AND (p_user_id IS NULL OR mo.user_id = p_user_id)
      AND (mo.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
    GROUP BY 1
),

daily_resource_counts AS (
    SELECT sub.d, jsonb_object_agg(sub.category, sub.cnt) AS resource_counts
    FROM (
        SELECT (r.created_at AT TIME ZONE 'UTC')::date AS d, r.category, COUNT(*) AS cnt
        FROM resources r
        WHERE r.deleted_at IS NULL
          AND r.category IS NOT NULL
          AND (p_user_id IS NULL OR r.user_id = p_user_id)
          AND (r.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
        GROUP BY 1, 2
    ) sub GROUP BY sub.d
),

-- 3. Sessions active on each date (for metadata) — window-bounded
daily_sessions AS (
    SELECT
        (m.created_at AT TIME ZONE 'UTC')::date AS d,
        jsonb_agg(DISTINCT jsonb_build_object(
            'session_id', s.id,
            'name', s.name,
            'agent_name', s.agent_name
        )) AS sessions
    FROM messages m
    JOIN sessions s ON s.id = m.session_id AND s.deleted_at IS NULL
    WHERE m.deleted_at IS NULL
      AND (p_user_id IS NULL OR m.user_id = p_user_id)
      AND (m.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
    GROUP BY 1
),

-- 4. Virtual daily summary cards — deterministic UUID from (user_id, date)
daily_summaries AS (
    SELECT
        'daily_summary'::varchar                                       AS event_type,
        uuid_generate_v5(
            'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid,
            COALESCE(p_user_id::text, 'global') || '/' || ad.d::text
        )                                                              AS event_id,
        ad.d                                                           AS event_date,
        (ad.d + TIME '23:59:59')::timestamptz                         AS event_timestamp,
        ('daily-' || ad.d::text)::varchar                              AS name,
        'daily_summary'::varchar                                       AS moment_type,
        format('%s: %s messages across %s session(s), %s tokens. %s moment(s).',
               CASE WHEN ad.d = CURRENT_DATE THEN 'Today'
                    WHEN ad.d = CURRENT_DATE - 1 THEN 'Yesterday'
                    ELSE to_char(ad.d, 'Mon DD')
               END,
               COALESCE(ds.msg_count, 0), COALESCE(ds.session_count, 0),
               COALESCE(ds.total_tokens, 0),
               COALESCE(dmc.moment_count, 0)
        )                                                              AS summary,
        uuid_generate_v5(
            'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::uuid,
            COALESCE(p_user_id::text, 'global') || '/' || ad.d::text
        )                                                              AS session_id,
        NULL::text                                                     AS image,
        NULL::varchar                                                  AS encryption_level,
        jsonb_build_object(
            'message_count', COALESCE(ds.msg_count, 0),
            'total_tokens', COALESCE(ds.total_tokens, 0),
            'session_count', COALESCE(ds.session_count, 0),
            'moment_count', COALESCE(dmc.moment_count, 0),
            'reminder_count', COALESCE(drc.reminder_count, 0),
            'resource_counts', COALESCE(drsc.resource_counts, '{}'::jsonb),
            'sessions', COALESCE(dss.sessions, '[]'::jsonb)
        )                                                              AS metadata
    FROM active_dates ad
    LEFT JOIN daily_stats ds ON ds.d = ad.d
    LEFT JOIN daily_moment_counts dmc ON dmc.d = ad.d
    LEFT JOIN daily_reminder_counts drc ON drc.d = ad.d
    LEFT JOIN daily_resource_counts drsc ON drsc.d = ad.d
    LEFT JOIN daily_sessions dss ON dss.d = ad.d
),

-- 5. Real moments — window-bounded, LEFT JOIN companion session
real_moments AS (
    SELECT
        'moment'::varchar                                              AS event_type,
        mo.id                                                          AS event_id,
        (mo.created_at AT TIME ZONE 'UTC')::date                      AS event_date,
        COALESCE(mo.starts_timestamp, mo.created_at)                   AS event_timestamp,
        mo.name::varchar                                               AS name,
        COALESCE(mo.moment_type, 'unknown')::varchar                   AS moment_type,
        mo.summary                                                     AS summary,
        mo.source_session_id                                           AS session_id,
        mo.image_uri                                                   AS image,
        mo.encryption_level                                            AS encryption_level,
        jsonb_build_object(
            'ends_timestamp', mo.ends_timestamp,
            'previous_moment_keys', mo.previous_moment_keys,
            'topic_tags', mo.topic_tags,
            'entities', mo.present_persons,
            'moment_metadata', mo.metadata,
            'session_name', s.name,
            'session_description', s.description,
            'session_metadata', s.metadata
        )                                                              AS metadata
    FROM moments mo
    LEFT JOIN sessions s ON s.id = mo.source_session_id AND s.deleted_at IS NULL
    WHERE mo.deleted_at IS NULL
      AND (p_user_id IS NULL OR mo.user_id = p_user_id)
      AND (mo.created_at AT TIME ZONE 'UTC')::date IN (SELECT d FROM active_dates)
      AND (p_include_future OR mo.starts_timestamp IS NULL OR mo.starts_timestamp <= CURRENT_TIMESTAMP)
),

-- 6. Combined feed — daily summaries sort before real moments on the same date
combined AS (
    SELECT *, 0 AS sort_priority FROM daily_summaries
    UNION ALL
    SELECT *, 1 AS sort_priority FROM real_moments
)

SELECT event_type, event_id, event_date, event_timestamp,
       name, moment_type, summary, session_id, image, encryption_level, metadata
FROM combined
ORDER BY event_date DESC, sort_priority ASC, event_timestamp DESC;
$$ LANGUAGE sql;


-- rem_fetch — batch entity retrieval by table and names
CREATE OR REPLACE FUNCTION rem_fetch(
    p_entities_by_table JSONB,
    p_user_id UUID DEFAULT NULL
) RETURNS TABLE(entity_type VARCHAR, data JSONB) AS $$
DECLARE
    v_table TEXT;
    v_keys JSONB;
BEGIN
    FOR v_table, v_keys IN
        SELECT * FROM jsonb_each(p_entities_by_table)
    LOOP
        RETURN QUERY EXECUTE format(
            'SELECT %L::varchar AS entity_type,
                    row_to_json(t.*)::jsonb AS data
             FROM %I t
             WHERE t.name = ANY(
                 SELECT jsonb_array_elements_text($1)
             )
             AND t.deleted_at IS NULL
             AND ($2 IS NULL OR t.user_id = $2)',
            v_table, v_table
        ) USING v_keys, p_user_id;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Triggers — all driven by schemas WHERE kind = 'table'
-- ---------------------------------------------------------------------------

-- updated_at auto-update — applied to ALL registered entity tables
-- Uses array collection to avoid cursor-vs-DDL conflicts during initdb.
DO $$
DECLARE
    v_tables TEXT[];
    v_table TEXT;
BEGIN
    SELECT array_agg(s.name) INTO v_tables
    FROM schemas s WHERE s.kind = 'table' AND s.deleted_at IS NULL;
    IF v_tables IS NULL THEN RETURN; END IF;

    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%I_updated_at ON %I;
             CREATE TRIGGER trg_%I_updated_at
                 BEFORE UPDATE ON %I
                 FOR EACH ROW EXECUTE FUNCTION update_updated_at()',
            v_table, v_table, v_table, v_table
        );
    END LOOP;
END;
$$;


-- KV store sync — applied to tables where has_kv_sync = true
DO $$
DECLARE
    v_tables TEXT[];
    v_table TEXT;
BEGIN
    SELECT array_agg(s.name) INTO v_tables
    FROM schemas s
    WHERE s.kind = 'table' AND s.deleted_at IS NULL
      AND (s.json_schema->>'has_kv_sync')::boolean = true;
    IF v_tables IS NULL THEN RETURN; END IF;

    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%I_kv ON %I;
             CREATE TRIGGER trg_%I_kv
                 AFTER INSERT OR UPDATE OR DELETE ON %I
                 FOR EACH ROW EXECUTE FUNCTION kv_store_upsert()',
            v_table, v_table, v_table, v_table
        );
    END LOOP;
END;
$$;


-- Embedding queue — applied to tables where has_embeddings = true
-- Each trigger passes the embedding_field as TG_ARGV[0]
DO $$
DECLARE
    v_tables TEXT[];
    v_fields TEXT[];
    v_table TEXT;
    v_field TEXT;
    i INT;
BEGIN
    SELECT array_agg(s.name), array_agg(s.json_schema->>'embedding_field')
    INTO v_tables, v_fields
    FROM schemas s
    WHERE s.kind = 'table' AND s.deleted_at IS NULL
      AND (s.json_schema->>'has_embeddings')::boolean = true
      AND s.json_schema->>'embedding_field' IS NOT NULL;
    IF v_tables IS NULL THEN RETURN; END IF;

    FOR i IN 1..array_length(v_tables, 1) LOOP
        v_table := v_tables[i];
        v_field := v_fields[i];
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_%I_embed ON %I;
             CREATE TRIGGER trg_%I_embed
                 AFTER INSERT OR UPDATE ON %I
                 FOR EACH ROW EXECUTE FUNCTION queue_embedding(%L)',
            v_table, v_table, v_table, v_table, v_field
        );
    END LOOP;
END;
$$;


-- Schema timemachine — audit trail for schemas table only
DROP TRIGGER IF EXISTS trg_schemas_timemachine ON schemas;
CREATE TRIGGER trg_schemas_timemachine
    AFTER INSERT OR UPDATE OR DELETE ON schemas
    FOR EACH ROW EXECUTE FUNCTION record_schema_timemachine();


-- ---------------------------------------------------------------------------
-- Indexes — all driven by schemas WHERE kind = 'table'
-- ---------------------------------------------------------------------------

-- HNSW vector indexes + content-hash indexes on embeddings tables
DO $$
DECLARE
    v_tables TEXT[];
    v_table TEXT;
BEGIN
    SELECT array_agg(s.name) INTO v_tables
    FROM schemas s
    WHERE s.kind = 'table' AND s.deleted_at IS NULL
      AND (s.json_schema->>'has_embeddings')::boolean = true;
    IF v_tables IS NULL THEN RETURN; END IF;

    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_embeddings_%I_hnsw
                 ON embeddings_%I USING hnsw (embedding vector_cosine_ops)',
            v_table, v_table
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_embeddings_%I_hash
                 ON embeddings_%I (content_hash)',
            v_table, v_table
        );
    END LOOP;
END;
$$;

-- KV store indexes (idx_kv_store_tenant_key already created with table above)
CREATE INDEX IF NOT EXISTS idx_kv_store_type ON kv_store (entity_type);
CREATE INDEX IF NOT EXISTS idx_kv_store_entity_id ON kv_store (entity_id);
CREATE INDEX IF NOT EXISTS idx_kv_store_graph ON kv_store USING GIN (graph_edges);
CREATE INDEX IF NOT EXISTS idx_kv_store_key_trgm ON kv_store USING GIN (entity_key gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_kv_store_summary_trgm ON kv_store USING GIN (content_summary gin_trgm_ops);

-- Entity table standard indexes (all kind='table' schemas)
-- Uses array collection to avoid holding a cursor on schemas while creating
-- indexes on it (which fails during initdb single-session execution).
DO $$
DECLARE
    v_tables TEXT[];
    v_table TEXT;
BEGIN
    SELECT array_agg(s.name)
    INTO v_tables
    FROM schemas s
    WHERE s.kind = 'table' AND s.deleted_at IS NULL;

    IF v_tables IS NULL THEN RETURN; END IF;

    FOREACH v_table IN ARRAY v_tables LOOP
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_tenant ON %I (tenant_id)',
                       v_table, v_table);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_created ON %I (created_at)',
                       v_table, v_table);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_metadata ON %I USING GIN (metadata)',
                       v_table, v_table);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_user ON %I (user_id)',
                       v_table, v_table);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_tags ON %I USING GIN (tags)',
                       v_table, v_table);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_graph_edges ON %I USING GIN (graph_edges)',
                       v_table, v_table);
    END LOOP;
END;
$$;

-- Messages: session lookup (hot path for chat history loading)
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at);

-- Messages: token budget queries
CREATE INDEX IF NOT EXISTS idx_messages_session_tokens ON messages (session_id, created_at DESC)
    INCLUDE (token_count);

-- Schemas: kind lookup (agent routing, type discovery, table registry queries)
CREATE INDEX IF NOT EXISTS idx_schemas_kind ON schemas (kind) WHERE deleted_at IS NULL;

-- Tools: server lookup
CREATE INDEX IF NOT EXISTS idx_tools_server ON tools (server_id) WHERE enabled = true;

-- Storage grants: sync worker lookup
CREATE INDEX IF NOT EXISTS idx_storage_grants_sync
    ON storage_grants (status, last_sync_at)
    WHERE auto_sync = true AND status = 'active';

-- Embedding queue: worker batch claim
CREATE INDEX IF NOT EXISTS idx_embedding_queue_pending
    ON embedding_queue (created_at ASC)
    WHERE status = 'pending';

-- Tenant keys: tenant lookup
CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_keys_tenant
    ON tenant_keys (tenant_id);

-- Redaction mappings: entity lookup (for de-anonymization)
CREATE INDEX IF NOT EXISTS idx_redaction_entity
    ON redaction_mappings (entity_table, entity_id);

-- Redaction mappings: session-scoped lookup
CREATE INDEX IF NOT EXISTS idx_redaction_session
    ON redaction_mappings (session_id) WHERE session_id IS NOT NULL;

-- Redaction mappings: tenant scoping
CREATE INDEX IF NOT EXISTS idx_redaction_tenant
    ON redaction_mappings (tenant_id);


-- ---------------------------------------------------------------------------
-- pg_cron Maintenance Jobs
-- ---------------------------------------------------------------------------

-- Embedding processor: pg_cron calls our API via pg_net every minute.
-- The API claims a batch from embedding_queue and processes it using the
-- configured provider (local for dev, openai for production).
-- Adjust the URL to match your P8_API_BASE_URL.
-- NOTE: pg_cron minimum interval is 1 minute. For sub-minute processing,
-- use the Python background worker (P8_EMBEDDING_WORKER_ENABLED=true).
-- Skipped when pg_net is not available (dev/test mode).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_net') THEN
        PERFORM cron.schedule('embed-process', '*/1 * * * *',
            'SELECT net.http_post(
                url := ''http://p8-api.p8.svc:8000/embeddings/process'',
                headers := jsonb_build_object(
                    ''Authorization'', ''Bearer '' || current_setting(''p8.api_key'', true),
                    ''Content-Type'', ''application/json''
                ),
                body := ''{}''::jsonb
            )');
    ELSE
        RAISE NOTICE 'pg_net not loaded — skipping embed-process cron job';
    END IF;
END $$;

-- Stale embedding retry: re-queue items stuck in 'processing' for > 5 minutes
SELECT cron.schedule('embed-retry', '*/2 * * * *', $$
    UPDATE embedding_queue
    SET status = 'pending'
    WHERE status = 'processing'
      AND created_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes';
$$);

-- KV store health check: hourly verify/repair
SELECT cron.schedule('kv-health', '0 * * * *', 'SELECT rebuild_kv_store_incremental()');

-- HNSW index maintenance: weekly reindex during low-traffic window
SELECT cron.schedule('hnsw-reindex', '0 3 * * 0', $$
    DO $inner$
    DECLARE
        v_rec RECORD;
    BEGIN
        FOR v_rec IN
            SELECT s.name AS table_name
            FROM schemas s
            WHERE s.kind = 'table' AND s.deleted_at IS NULL
              AND (s.json_schema->>'has_embeddings')::boolean = true
        LOOP
            EXECUTE format('REINDEX INDEX CONCURRENTLY idx_embeddings_%I_hnsw', v_rec.table_name);
        END LOOP;
    END;
    $inner$;
$$);

-- Dropped column GC: weekly cleanup of staged column drops
SELECT cron.schedule('gc-dropped-cols', '0 3 * * 0', 'SELECT gc_dropped_columns(30)');


-- ---------------------------------------------------------------------------
-- Seed Data
-- ---------------------------------------------------------------------------

-- Test user (idempotent — deterministic ID from email)
INSERT INTO users (id, name, email, content, metadata, tags, user_id)
VALUES (
    p8_deterministic_id('users', 'user@example.com'),
    'Sage Whitfield',
    'user@example.com',
    'Default test user for development and integration testing.',
    '{
      "env": "dev", "role": "admin",
      "relations": [
        {"name": "Cedar", "role": "pet", "notes": "Border collie, loves trail runs"},
        {"name": "Rowan", "role": "friend", "notes": "Birding partner, met at Audubon Society"}
      ],
      "interests": [
        "forest ecology", "birdwatching", "mushroom foraging", "trail running",
        "woodworking", "field recording", "wildlife photography", "permaculture"
      ],
      "feeds": [
        {"url": "https://www.audubon.org/news/rss", "name": "Audubon News", "type": "rss"},
        {"url": "https://www.treehugger.com/feeds/all", "name": "Treehugger", "type": "rss"},
        {"url": "https://www.inaturalist.org/observations.atom", "name": "iNaturalist", "type": "rss"},
        {"url": "https://www.fs.usda.gov/news/releases", "name": "US Forest Service", "type": "website"}
      ],
      "preferences": {"timezone": "US/Pacific", "language": "en", "summary_style": "concise"},
      "facts": {
        "location": "Pacific Northwest", "birthday": "June 21",
        "occupation": "Restoration ecologist",
        "favorite_trail": "Eagle Creek Trail, Columbia River Gorge",
        "birding_life_list": "247 species", "favorite_tree": "Western red cedar"
      }
    }'::jsonb,
    ARRAY['dev', 'test'],
    '7d31eddf-7ff7-542a-982f-7522e7a3ec67'::uuid
)
ON CONFLICT (id) DO UPDATE SET
    name     = EXCLUDED.name,
    content  = EXCLUDED.content,
    metadata = EXCLUDED.metadata,
    tags     = EXCLUDED.tags;
