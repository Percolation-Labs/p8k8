-- =============================================================================
-- 05_percolate.sql — Percolate detection core: seed sources, ingestion scheduling
--
-- Table DDL for percolate_sources/entities/topics/events lives in
-- 01_install_entities.sql (they're ordinary CoreModel entity tables, registered
-- in schemas like everything else). This file only adds domain-specific seed
-- data and pg_cron scheduling, matching the enqueue_*_tasks() pattern in
-- sql/03_qms.sql.
--
-- Run AFTER 01_install_entities.sql (needs the tables + p8_deterministic_id)
-- and 03_qms.sql (needs task_queue + cron extension).
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Seed the v1 source set (§15.1 first pass — zero-auth, free)
-- ---------------------------------------------------------------------------

INSERT INTO percolate_sources (
    id, key, name, source_type, domain, base_url, auth_required, reliability_weight, description
) VALUES
(p8_deterministic_id('percolate_sources', 'sec_edgar'),
 'sec_edgar', 'SEC EDGAR Full-Text Search', 'regulatory_filing', 'finance',
 'https://efts.sec.gov/LATEST/search-index', false, 1.0,
 'US public company filings. Requires a User-Agent header, no other auth.'),

(p8_deterministic_id('percolate_sources', 'arxiv'),
 'arxiv', 'arXiv API', 'research_paper', 'research',
 'https://export.arxiv.org/api/query', false, 0.9,
 'Research/tech leading indicators. Must use https, not http.'),

(p8_deterministic_id('percolate_sources', 'hn'),
 'hn', 'Hacker News Firebase API', 'social_chatter', 'dev_tech',
 'https://hacker-news.firebaseio.com/v0', false, 0.6,
 'Dev/tech/startup subculture signal.'),

(p8_deterministic_id('percolate_sources', 'federal_register'),
 'federal_register', 'Federal Register API', 'regulatory_notice', 'regulatory',
 'https://www.federalregister.gov/api/v1', false, 1.0,
 'US regulatory/policy events.'),

(p8_deterministic_id('percolate_sources', 'wikipedia_recent_changes'),
 'wikipedia_recent_changes', 'Wikipedia RecentChanges API', 'attention_signal', 'general',
 'https://en.wikipedia.org/w/api.php', false, 0.4,
 'Broad, noisy general-world-event proxy via edit velocity.')

ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    source_type = EXCLUDED.source_type,
    domain = EXCLUDED.domain,
    base_url = EXCLUDED.base_url,
    reliability_weight = EXCLUDED.reliability_weight,
    description = EXCLUDED.description;


-- Seed one topic per source domain — events attach to these via "about" edges.
INSERT INTO percolate_topics (id, name, description) VALUES
(p8_deterministic_id('percolate_topics', 'finance'), 'finance', 'Corporate/finance events — regulatory filings, market signals.'),
(p8_deterministic_id('percolate_topics', 'research'), 'research', 'Research/tech leading indicators — papers, preprints.'),
(p8_deterministic_id('percolate_topics', 'dev_tech'), 'dev_tech', 'Developer/tech/startup subculture.'),
(p8_deterministic_id('percolate_topics', 'regulatory'), 'regulatory', 'US regulatory/policy events.'),
(p8_deterministic_id('percolate_topics', 'general'), 'general', 'General world-event attention signal.')
ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description;


-- ---------------------------------------------------------------------------
-- enqueue_percolate_ingest_tasks — called by pg_cron every 30 minutes.
-- One task per enabled source, deduped against pending/processing/recently
-- completed tasks for that source so a slow run doesn't pile up duplicates.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enqueue_percolate_ingest_tasks() RETURNS INT AS $$
DECLARE
    v_count INT := 0;
    v_source RECORD;
BEGIN
    FOR v_source IN
        SELECT key FROM percolate_sources WHERE enabled = true AND deleted_at IS NULL
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM task_queue tq
            WHERE tq.task_type = 'percolate_ingest'
              AND tq.payload->>'source_key' = v_source.key
              AND (
                  tq.status IN ('pending', 'processing')
                  OR (tq.status = 'completed' AND tq.completed_at > CURRENT_TIMESTAMP - INTERVAL '25 minutes')
              )
        ) THEN
            INSERT INTO task_queue (task_type, tier, payload)
            VALUES (
                'percolate_ingest',
                'small',
                jsonb_build_object('source_key', v_source.key, 'trigger', 'scheduled', 'enqueued_at', CURRENT_TIMESTAMP)
            );
            v_count := v_count + 1;
        END IF;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

SELECT cron.schedule('qms-percolate-ingest-enqueue', '*/30 * * * *', 'SELECT enqueue_percolate_ingest_tasks()');


-- ---------------------------------------------------------------------------
-- Feed query indexes — GET /percolate/feed orders by status + corroboration
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_percolate_events_feed
    ON percolate_events (status, corroboration_score DESC, event_time DESC)
    WHERE deleted_at IS NULL;
