# QMS — Queue Management System

Tiered background processing for the Percolate stack. This includes running file processing tasks or memory building tasks. Scheduling is also managed. This leans heavily on Postgres services like cron and exposing postgres queues to KEDA. This is reasonably sclable for enterprise apps but we could use other queue provides like NATs in a similar way. We integrate user plans and their quotas into queue processing logic for example a user can have a dreaming budget or a budget for how much file processing (or how big files).

For example, the file processor is described next. 

## Architecture

```
Upload → S3 + File entity → enqueue task → Worker claims → Process → Track usage
                                              ↑
                              KEDA polls task_queue per tier
```

Single `task_queue` table in PostgreSQL. Workers use `FOR UPDATE SKIP LOCKED` for contention-free claiming. Same Docker image for all workers — command override selects tier.

## Tiers

| Tier | Replicas | CPU req/lim | Mem req/lim | KEDA | Use Case |
|------|----------|-------------|-------------|------|----------|
| micro | 1 (fixed) | 50m/250m | 128Mi/256Mi | No | Scheduled tasks, lightweight ops |
| small | 0→5 | 250m/500m | 512Mi/1Gi | Yes | Files <1MB, dreaming |
| medium | 0→3 | 500m/1000m | 1Gi/2Gi | Yes | Files 1–50MB |
| large | 0→2 | 1000m/2000m | 2Gi/4Gi | Yes | Files >=50MB |

## Task Types

- **file_processing**: Download from S3 → extract text → chunk → persist resources
- **dreaming**: Per-user background AI — moment consolidation and insights
- **scheduled**: KV rebuild, embedding backfill, maintenance

## Retry Strategy

Exponential backoff: 30s, 2m, 8m (30s × 4^retry). Default 3 retries. Stale task recovery every 5 minutes via pg_cron.

## PostgreSQL Image

Custom build (`percolationlabs/rem-pg:18`) based on CNPG PG18 with:
- **pgvector** v0.8.1 — vector similarity search for embeddings
- **pg_cron** v1.6.7 — in-database job scheduling (no K8s CronJobs needed)

Built from `docker/Dockerfile.pg18`. Requires `shared_preload_libraries = 'pg_cron'` and `cron.database_name` in PostgreSQL config.

## pg_cron Scheduling

All periodic scheduling runs inside PostgreSQL via pg_cron — no K8s CronJobs:

| Job | Schedule | Function |
|-----|----------|----------|
| `qms-recover-stale` | Every 5 min | `recover_stale_tasks(15)` — reset stuck processing tasks |
| `qms-dreaming-enqueue` | Hourly | `enqueue_dreaming_tasks()` — enqueue dreaming for active users |

## Quota Integration

Plans: free, pro, team, enterprise. Each defines:
- `max_file_size_bytes` — per-file upload limit
- `worker_bytes_processed` — monthly file processing budget
- `dreaming_io_tokens` — monthly dreaming token budget

Workers check quotas before processing and track usage after completion.

## Key Files

| File | Purpose |
|------|---------|
| `sql/qms.sql` | Table, functions, indexes, pg_cron jobs |
| `services/queue.py` | QueueService — Python API |
| `services/usage.py` | PlanLimits + UsageService |
| `workers/processor.py` | TieredWorker with handler registry |
| `workers/handlers/` | file_processing, dreaming, scheduled |
| `tests/test_queue.py` | Integration tests |

## Running Workers

```bash
# Local development
python -m workers.processor --tier small --poll-interval 2 --batch-size 3

# Docker / K8s (same image, different command)
python -m workers.processor --tier medium --poll-interval 10 --batch-size 1
```

## Admin API

```
GET /admin/queue/stats → {"small/pending": 3, "medium/processing": 1, ...}
```

## KEDA Scaling

Each ScaledObject polls every 15s with 60s cooldown:
```sql
SELECT COALESCE(COUNT(*), 0) FROM task_queue
WHERE status = 'pending' AND tier = '$TIER' AND scheduled_at <= CURRENT_TIMESTAMP
```
