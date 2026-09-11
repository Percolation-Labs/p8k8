-- =============================================================================
-- 05_retire_percolate.sql — take the event-detection pipeline out of a
-- database that ran it
--
-- p8k8 carried an event-detection pipeline (sources, entity resolution,
-- corroboration, an event/entity graph) built as the first stage of a product
-- that is no longer being pursued. Its code is gone; this file stops a
-- database that ran it from still scheduling work for it. Without it, pg_cron
-- would go on enqueueing a 'percolate_ingest' task every 30 minutes for a
-- handler no worker registers, and each one would fail with "No handler
-- registered".
--
-- The tables it filled (percolate_sources/entities/topics/events and their
-- embeddings_* companions) are left in place with their rows, and so are their
-- rows in `schemas`: dropping data is a decision for whoever owns it, not a
-- side effect of a deploy. To remove them:
--   DROP TABLE embeddings_percolate_events, embeddings_percolate_topics,
--              embeddings_percolate_entities, percolate_events,
--              percolate_topics, percolate_entities, percolate_sources;
--   DELETE FROM schemas WHERE kind = 'table' AND name LIKE 'percolate\_%';
--
-- Safe to run on every deploy and on a database that never had any of it.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        PERFORM cron.unschedule(jobid) FROM cron.job
         WHERE jobname = 'qms-percolate-ingest-enqueue';
    END IF;
END $$;

DROP FUNCTION IF EXISTS enqueue_percolate_ingest_tasks();

-- Work already queued for it would only fail. Completed and failed rows are
-- history and stay.
DELETE FROM task_queue WHERE task_type = 'percolate_ingest' AND status = 'pending';
DELETE FROM embedding_queue WHERE table_name LIKE 'percolate\_%' AND status = 'pending';
