-- =============================================================================
-- 03_qms.sql — Queue Management System: task_queue table, functions, indexes, cron
--
-- Runs AFTER 02_install.sql. Provides a unified background processing queue
-- with tiered workers (micro/small/medium/large), retry with exponential
-- backoff, and pg_cron scheduled enqueuing.
--
-- Pattern: FOR UPDATE SKIP LOCKED (proven in EmbeddingService)
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Task Queue Table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_type       VARCHAR(50) NOT NULL,     -- file_processing | dreaming | news | scheduled
    tier            VARCHAR(20) NOT NULL DEFAULT 'small',  -- micro | small | medium | large
    tenant_id       VARCHAR(100),
    user_id         UUID,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority        INT NOT NULL DEFAULT 0,
    scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at      TIMESTAMPTZ,
    claimed_by      VARCHAR(100),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error           TEXT,
    retry_count     INT NOT NULL DEFAULT 0,
    max_retries     INT NOT NULL DEFAULT 3,
    result          JSONB,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);


-- ---------------------------------------------------------------------------
-- Task Events — append-only audit log for task lifecycle
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS task_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL,
    task_type       VARCHAR(50),
    user_id         UUID,
    event           VARCHAR(30) NOT NULL,   -- claimed | completed | failed | retrying | recovered | quota_exceeded | error
    worker_id       VARCHAR(100),
    error           TEXT,
    detail          JSONB,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_user    ON task_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_events_event   ON task_events (event, created_at DESC);

-- Helper to emit a task event
CREATE OR REPLACE FUNCTION emit_task_event(
    p_task_id UUID,
    p_event VARCHAR,
    p_worker_id VARCHAR DEFAULT NULL,
    p_error TEXT DEFAULT NULL,
    p_detail JSONB DEFAULT NULL
) RETURNS VOID AS $$
DECLARE
    v_task_type VARCHAR;
    v_user_id UUID;
BEGIN
    SELECT task_type, user_id INTO v_task_type, v_user_id
    FROM task_queue WHERE id = p_task_id;

    INSERT INTO task_events (task_id, task_type, user_id, event, worker_id, error, detail)
    VALUES (p_task_id, v_task_type, v_user_id, p_event, p_worker_id, p_error, p_detail);
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Indexes — partial indexes per tier for KEDA queries + claim performance
-- ---------------------------------------------------------------------------

-- Per-tier pending counts (KEDA polls these)
CREATE INDEX IF NOT EXISTS idx_task_queue_pending_micro
    ON task_queue (scheduled_at ASC)
    WHERE status = 'pending' AND tier = 'micro';

CREATE INDEX IF NOT EXISTS idx_task_queue_pending_small
    ON task_queue (scheduled_at ASC)
    WHERE status = 'pending' AND tier = 'small';

CREATE INDEX IF NOT EXISTS idx_task_queue_pending_medium
    ON task_queue (scheduled_at ASC)
    WHERE status = 'pending' AND tier = 'medium';

CREATE INDEX IF NOT EXISTS idx_task_queue_pending_large
    ON task_queue (scheduled_at ASC)
    WHERE status = 'pending' AND tier = 'large';

-- Claim query: priority DESC, scheduled_at ASC within a tier
CREATE INDEX IF NOT EXISTS idx_task_queue_claim
    ON task_queue (tier, priority DESC, scheduled_at ASC)
    WHERE status = 'pending';

-- Stale task recovery
CREATE INDEX IF NOT EXISTS idx_task_queue_processing
    ON task_queue (claimed_at)
    WHERE status = 'processing';

-- User-scoped queries (quota checks, dreaming)
CREATE INDEX IF NOT EXISTS idx_task_queue_user
    ON task_queue (user_id, task_type, status);

-- updated_at trigger
DROP TRIGGER IF EXISTS trg_task_queue_updated_at ON task_queue;
CREATE TRIGGER trg_task_queue_updated_at
    BEFORE UPDATE ON task_queue
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ---------------------------------------------------------------------------
-- Core Functions
-- ---------------------------------------------------------------------------

-- claim_tasks — atomically claim a batch of pending tasks for a worker tier.
-- Uses FOR UPDATE SKIP LOCKED to avoid contention between workers.
CREATE OR REPLACE FUNCTION claim_tasks(
    p_tier VARCHAR,
    p_worker_id VARCHAR,
    p_batch_size INT DEFAULT 1
) RETURNS SETOF task_queue AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        UPDATE task_queue
        SET status = 'processing',
            claimed_at = CURRENT_TIMESTAMP,
            claimed_by = p_worker_id,
            started_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM task_queue
            WHERE status = 'pending'
              AND tier = p_tier
              AND scheduled_at <= CURRENT_TIMESTAMP
            ORDER BY priority DESC, scheduled_at ASC
            LIMIT p_batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
    )
    SELECT * FROM claimed;

    -- Log claimed events
    INSERT INTO task_events (task_id, task_type, user_id, event, worker_id)
    SELECT id, task_type, user_id, 'claimed', p_worker_id
    FROM task_queue
    WHERE claimed_by = p_worker_id AND status = 'processing'
      AND claimed_at >= CURRENT_TIMESTAMP - INTERVAL '5 seconds';
END;
$$ LANGUAGE plpgsql;


-- complete_task — mark a task as completed with optional result payload.
CREATE OR REPLACE FUNCTION complete_task(
    p_task_id UUID,
    p_result JSONB DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    UPDATE task_queue
    SET status = 'completed',
        completed_at = CURRENT_TIMESTAMP,
        result = p_result
    WHERE id = p_task_id;

    PERFORM emit_task_event(p_task_id, 'completed', NULL, NULL, p_result);
END;
$$ LANGUAGE plpgsql;


-- fail_task — mark a task as failed. Auto-retries with exponential backoff
-- (30s, 2m, 8m) up to max_retries, then marks permanently failed.
CREATE OR REPLACE FUNCTION fail_task(
    p_task_id UUID,
    p_error TEXT
) RETURNS VOID AS $$
DECLARE
    v_retry_count INT;
    v_max_retries INT;
    v_backoff INTERVAL;
BEGIN
    SELECT retry_count, max_retries INTO v_retry_count, v_max_retries
    FROM task_queue WHERE id = p_task_id;

    IF v_retry_count < v_max_retries THEN
        -- Exponential backoff: 30s * 4^retry_count (30s, 2m, 8m, 32m, ...)
        v_backoff := (30 * power(4, v_retry_count)) * INTERVAL '1 second';
        UPDATE task_queue
        SET status = 'pending',
            error = p_error,
            retry_count = retry_count + 1,
            scheduled_at = CURRENT_TIMESTAMP + v_backoff,
            claimed_at = NULL,
            claimed_by = NULL,
            started_at = NULL
        WHERE id = p_task_id;

        PERFORM emit_task_event(p_task_id, 'retrying', NULL, p_error,
            jsonb_build_object('retry', v_retry_count + 1, 'max_retries', v_max_retries,
                               'next_attempt', CURRENT_TIMESTAMP + v_backoff));
    ELSE
        UPDATE task_queue
        SET status = 'failed',
            error = p_error,
            completed_at = CURRENT_TIMESTAMP
        WHERE id = p_task_id;

        PERFORM emit_task_event(p_task_id, 'failed', NULL, p_error,
            jsonb_build_object('retry', v_retry_count, 'max_retries', v_max_retries));
    END IF;
END;
$$ LANGUAGE plpgsql;


-- recover_stale_tasks — reset tasks stuck in 'processing' beyond timeout.
-- Called by pg_cron every few minutes for self-healing.
CREATE OR REPLACE FUNCTION recover_stale_tasks(
    p_timeout_minutes INT DEFAULT 15
) RETURNS INT AS $$
DECLARE
    v_count INT;
BEGIN
    -- Log recovered events before updating
    INSERT INTO task_events (task_id, task_type, user_id, event, worker_id, error, detail)
    SELECT id, task_type, user_id, 'recovered', claimed_by,
           'processing timeout after ' || p_timeout_minutes || ' minutes',
           jsonb_build_object('retry', retry_count + 1, 'claimed_at', claimed_at)
    FROM task_queue
    WHERE status = 'processing'
      AND claimed_at < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::interval
      AND retry_count < max_retries;

    UPDATE task_queue
    SET status = 'pending',
        error = 'recovered: processing timeout after ' || p_timeout_minutes || ' minutes',
        retry_count = retry_count + 1,
        scheduled_at = CURRENT_TIMESTAMP,
        claimed_at = NULL,
        claimed_by = NULL,
        started_at = NULL
    WHERE status = 'processing'
      AND claimed_at < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::interval
      AND retry_count < max_retries;

    GET DIAGNOSTICS v_count = ROW_COUNT;

    -- Log permanently failed events before updating
    INSERT INTO task_events (task_id, task_type, user_id, event, worker_id, error, detail)
    SELECT id, task_type, user_id, 'failed', claimed_by,
           'exceeded max retries after processing timeout',
           jsonb_build_object('retry', retry_count, 'max_retries', max_retries, 'claimed_at', claimed_at)
    FROM task_queue
    WHERE status = 'processing'
      AND claimed_at < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::interval
      AND retry_count >= max_retries;

    -- Mark permanently failed if max retries exceeded
    UPDATE task_queue
    SET status = 'failed',
        error = 'recovered: exceeded max retries after processing timeout',
        completed_at = CURRENT_TIMESTAMP
    WHERE status = 'processing'
      AND claimed_at < CURRENT_TIMESTAMP - (p_timeout_minutes || ' minutes')::interval
      AND retry_count >= max_retries;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- enqueue_file_task — auto-assigns tier by file size.
-- <1MB = small, <50MB = medium, >=50MB = large
CREATE OR REPLACE FUNCTION enqueue_file_task(
    p_file_id UUID,
    p_user_id UUID DEFAULT NULL,
    p_tenant_id VARCHAR DEFAULT NULL
) RETURNS UUID AS $$
DECLARE
    v_size_bytes BIGINT;
    v_tier VARCHAR;
    v_task_id UUID;
    v_uri TEXT;
    v_name TEXT;
    v_mime TEXT;
BEGIN
    SELECT size_bytes, uri, name, mime_type
    INTO v_size_bytes, v_uri, v_name, v_mime
    FROM files WHERE id = p_file_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'File % not found', p_file_id;
    END IF;

    -- Auto-assign tier by file size
    IF COALESCE(v_size_bytes, 0) < 1048576 THEN         -- <1MB
        v_tier := 'small';
    ELSIF v_size_bytes < 52428800 THEN                    -- <50MB
        v_tier := 'medium';
    ELSE                                                   -- >=50MB
        v_tier := 'large';
    END IF;

    INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload, priority)
    VALUES (
        'file_processing',
        v_tier,
        p_user_id,
        p_tenant_id,
        jsonb_build_object(
            'file_id', p_file_id,
            'uri', v_uri,
            'name', v_name,
            'mime_type', v_mime,
            'size_bytes', v_size_bytes
        ),
        CASE WHEN COALESCE(v_size_bytes, 0) < 1048576 THEN 10 ELSE 0 END
    )
    RETURNING id INTO v_task_id;

    RETURN v_task_id;
END;
$$ LANGUAGE plpgsql;


-- enqueue_dreaming_tasks — called by pg_cron every 12 hours.
-- Creates one dreaming task per user who has had activity since their last
-- dreaming run (or ever, if no prior dreaming task exists).
-- Enforces a 12-hour cooldown: won't re-enqueue if a dreaming task completed
-- within the last 12 hours.
-- Activity = new messages OR new processed file uploads.
-- Uses COALESCE(u.user_id, u.id) because sessions reference the auth-level
-- user_id when set, falling back to the row id.
CREATE OR REPLACE FUNCTION enqueue_dreaming_tasks() RETURNS INT AS $$
DECLARE
    v_count INT := 0;
    v_user RECORD;
BEGIN
    FOR v_user IN
        WITH last_dreaming AS (
            SELECT tq.user_id, MAX(tq.created_at) AS last_run
            FROM task_queue tq
            WHERE tq.task_type = 'dreaming'
              AND tq.status IN ('completed', 'processing', 'pending')
            GROUP BY tq.user_id
        ),
        active_users AS (
            -- Users with new messages since last dreaming
            SELECT DISTINCT COALESCE(u.user_id, u.id) AS effective_uid, u.tenant_id
            FROM users u
            JOIN sessions s ON s.user_id = COALESCE(u.user_id, u.id) AND s.deleted_at IS NULL
            JOIN messages m ON m.session_id = s.id AND m.deleted_at IS NULL
            WHERE u.deleted_at IS NULL
              AND m.created_at > COALESCE(
                  (SELECT ld.last_run FROM last_dreaming ld WHERE ld.user_id = COALESCE(u.user_id, u.id)),
                  CURRENT_TIMESTAMP - INTERVAL '24 hours'
              )

            UNION

            -- Users with new processed file uploads since last dreaming
            SELECT DISTINCT COALESCE(u.user_id, u.id) AS effective_uid, u.tenant_id
            FROM users u
            JOIN files f ON f.user_id = COALESCE(u.user_id, u.id)
                        AND f.deleted_at IS NULL
                        AND f.processing_status = 'completed'
            WHERE u.deleted_at IS NULL
              AND f.created_at > COALESCE(
                  (SELECT ld.last_run FROM last_dreaming ld WHERE ld.user_id = COALESCE(u.user_id, u.id)),
                  CURRENT_TIMESTAMP - INTERVAL '24 hours'
              )
        )
        SELECT effective_uid, tenant_id FROM active_users
        WHERE NOT EXISTS (
            -- Skip if there's a pending/processing task OR a completed one within 12 hours
            SELECT 1 FROM task_queue tq
            WHERE tq.task_type = 'dreaming'
              AND tq.user_id = active_users.effective_uid
              AND (
                  tq.status IN ('pending', 'processing')
                  OR (tq.status = 'completed' AND tq.completed_at > CURRENT_TIMESTAMP - INTERVAL '12 hours')
              )
        )
    LOOP
        INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload)
        VALUES (
            'dreaming',
            'small',
            v_user.effective_uid,
            v_user.tenant_id,
            jsonb_build_object('trigger', 'scheduled', 'enqueued_at', CURRENT_TIMESTAMP)
        );
        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- enqueue_news_tasks — called by pg_cron daily.
-- Creates one news task per user who has interests, categories, or feeds
-- in metadata and hasn't had a news task today.
-- The news task is handled by ReadingSummaryHandler which fetches feeds,
-- upserts resources, and creates a reading moment with LLM summary.
CREATE OR REPLACE FUNCTION enqueue_news_tasks() RETURNS INT AS $$
DECLARE
    v_count INT := 0;
    v_user RECORD;
BEGIN
    FOR v_user IN
        SELECT COALESCE(u.user_id, u.id) AS effective_uid, u.tenant_id
        FROM users u
        WHERE u.deleted_at IS NULL
          AND u.metadata IS NOT NULL
          AND (
              u.metadata->>'interests' IS NOT NULL
              OR u.metadata->>'categories' IS NOT NULL
              OR u.metadata->>'feeds' IS NOT NULL
          )
          -- Skip users who already have a pending/processing/completed news task today
          AND NOT EXISTS (
              SELECT 1 FROM task_queue tq
              WHERE tq.task_type = 'news'
                AND tq.user_id = COALESCE(u.user_id, u.id)
                AND tq.created_at >= date_trunc('day', CURRENT_TIMESTAMP)
                AND tq.status IN ('pending', 'processing', 'completed')
          )
    LOOP
        INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload)
        VALUES (
            'news',
            'small',
            v_user.effective_uid,
            v_user.tenant_id,
            jsonb_build_object('trigger', 'scheduled', 'enqueued_at', CURRENT_TIMESTAMP)
        );
        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- enqueue_drive_sync_tasks — called by pg_cron hourly at :30.
-- Creates one drive_sync task per user who has an active Google Drive grant
-- with auto_sync=true and a folder selected, and hasn't been synced recently.
CREATE OR REPLACE FUNCTION enqueue_drive_sync_tasks() RETURNS INT AS $$
DECLARE
    v_count INT := 0;
    v_grant RECORD;
BEGIN
    FOR v_grant IN
        SELECT sg.user_id_ref AS user_id, sg.tenant_id, sg.provider_folder_id
        FROM storage_grants sg
        WHERE sg.provider = 'google-drive'
          AND sg.status = 'active'
          AND sg.auto_sync = true
          AND sg.provider_folder_id IS NOT NULL
          -- Skip users with pending/processing tasks or completed within last hour
          AND NOT EXISTS (
              SELECT 1 FROM task_queue tq
              WHERE tq.task_type = 'drive_sync'
                AND tq.user_id = sg.user_id_ref
                AND (
                    tq.status IN ('pending', 'processing')
                    OR (tq.status = 'completed' AND tq.completed_at > CURRENT_TIMESTAMP - INTERVAL '1 hour')
                )
          )
    LOOP
        INSERT INTO task_queue (task_type, tier, user_id, tenant_id, payload)
        VALUES (
            'drive_sync',
            'small',
            v_grant.user_id,
            v_grant.tenant_id,
            jsonb_build_object(
                'trigger', 'scheduled',
                'folder_id', v_grant.provider_folder_id,
                'enqueued_at', CURRENT_TIMESTAMP
            )
        );
        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- pg_cron Jobs
-- ---------------------------------------------------------------------------

-- Stale task recovery: every 5 minutes, reset stuck tasks
SELECT cron.schedule('qms-recover-stale', '*/5 * * * *', 'SELECT recover_stale_tasks(15)');

-- Dreaming enqueue: every 12 hours (6am and 6pm UTC)
SELECT cron.schedule('qms-dreaming-enqueue', '0 6,18 * * *', 'SELECT enqueue_dreaming_tasks()');

-- News feed: daily at 6am UTC, enqueue news digest for users with interests
SELECT cron.schedule('qms-news-enqueue', '0 6 * * *', 'SELECT enqueue_news_tasks()');

-- Drive sync: hourly at :30, enqueue sync for users with auto_sync enabled
SELECT cron.schedule('qms-drive-sync-enqueue', '30 * * * *', 'SELECT enqueue_drive_sync_tasks()');
