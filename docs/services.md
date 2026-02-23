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
- **news**: Daily feed digest per user (interests/categories in user metadata)
- **reading_summary**: Summarize a user's daily reading moment (on-demand)
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
| `qms-news-enqueue` | Daily 06:00 UTC | `enqueue_news_tasks()` — enqueue news digest for users with interests |

## Quota Integration

Plans: free, pro, team, enterprise. Each defines:
- `max_file_size_bytes` — per-file upload limit
- `worker_bytes_processed` — monthly file processing budget
- `dreaming_io_tokens` — monthly dreaming token budget

Workers check quotas before processing and track usage after completion.

## Key Files

| File | Purpose |
|------|---------|
| `sql/03_qms.sql` | Table, functions, indexes, pg_cron jobs |
| `services/queue.py` | QueueService — Python API |
| `services/usage.py` | PlanLimits + UsageService |
| `workers/processor.py` | TieredWorker with handler registry |
| `workers/handlers/` | file_processing, dreaming, news, reading, scheduled |
| `api/cli/admin.py` | Admin CLI — health, queue, quota |
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

## Admin CLI

`p8 admin` provides four commands for monitoring and operating the queue system.

### `p8 admin health` — Pipeline diagnostics

The primary tool. Shows the full processing pipeline status and per-user task health.

```bash
p8 admin health                        # All users
p8 admin health --email amartey@...    # Single user by email (partial match, decrypts)
p8 admin health --user <UUID>          # Single user by id or user_id
```

Output:
1. **Processing Pipeline** — five-stage check:
   - **pg_net GUC**: Is `p8.internal_api_url` set? (required for all pg_cron HTTP jobs)
   - **pg_net jobs**: Do reminder jobs use GUC-based URLs? (detects hardcoded legacy jobs)
   - **pg_cron**: Are `qms-dreaming-enqueue` and `qms-news-enqueue` active and succeeding?
   - **task_queue**: How many tasks are due now, by tier?
   - **workers**: Have any workers ever claimed a task? When was the last claim?
2. **Per-user task table** — for each task type (dreaming, news, reading_summary, file_processing, scheduled):
   - Pending count + when due + age
   - Last run time + status (completed/failed)
   - Issues: OVERDUE, NO WORKERS, STUCK processing, last failed error, never scheduled
3. **pg_cron Jobs** — all jobs with last run time, status, and result

The pipeline section explains exactly where to look when tasks aren't executing:
```
pg_cron enqueues → KEDA polls task_queue → scales worker deployment 0→N → worker claims
```

### `p8 admin queue` — Raw queue inspection

```bash
p8 admin queue                         # Pending tasks aggregated by tenant + type
p8 admin queue --status failed         # Failed tasks (the dangerous silent failures)
p8 admin queue --status completed      # Completed tasks
p8 admin queue --detail                # Individual tasks with IDs, tier, retries, age
p8 admin queue --detail --type dreaming --status failed   # Drill down
```

### `p8 admin quota` — Usage report and reset

```bash
p8 admin quota                         # All users with progress bars
p8 admin quota --user <UUID>           # Single user
p8 admin quota --reset --user <UUID>   # Reset all current-period counters
p8 admin quota --reset --user <UUID> --resource chat_tokens  # Reset one resource
```

### `p8 admin enqueue` — Manually trigger a task

```bash
p8 admin enqueue reading_summary --user <UUID>             # Run immediately
p8 admin enqueue dreaming --user <UUID> --delay 5          # Run in 5 minutes
p8 admin enqueue news --user <UUID>                        # News digest
```

Valid task types: `dreaming`, `news`, `reading_summary`, `file_processing`, `scheduled`.

### Connecting to remote (default)

All admin commands default to **remote** (Hetzner p8-w-1 via port-forward). Open the port-forwards first:

```bash
kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw 5490:5432 &
kubectl --context=p8-w-1 -n p8 port-forward svc/openbao 8201:8200 &    # optional, for email decryption
```

Standard ports: **5490** (Postgres), **8201** (Vault/OpenBao).

If the port-forward isn't open, admin commands exit with instructions. If `P8_DATABASE_URL` is already set to a remote URL (not `localhost:5488`), it's used as-is.

For the local docker-compose DB, use `--local`:

```bash
p8 admin --local health
```

If credentials are needed (production DB has auth), set the env:

```bash
PG_PASS=$(kubectl --context=p8-w-1 -n p8 get secret p8-database-credentials \
  -o jsonpath='{.data.password}' | base64 -d)

P8_DATABASE_URL="postgresql://p8user:${PG_PASS}@localhost:5490/p8db?sslmode=disable" \
  p8 admin health --email amartey
```

## Troubleshooting: Tasks not executing

The processing pipeline has four stages. Check each in order:

| Stage | What to check | Fix |
|-------|--------------|-----|
| **1. pg_cron** | `p8 admin health` → pg_cron table. Jobs active? Last run succeeded? | `SELECT cron.schedule(...)` to recreate missing jobs. Run `p8 migrate` to re-apply. |
| **2. task_queue** | `p8 admin queue --status pending`. Tasks due? Or all scheduled in future? | If no pending tasks, the enqueue function conditions aren't met (no user activity, no interests metadata, etc.) |
| **3. KEDA** | `kubectl get scaledobject -n p8` — is it READY? `kubectl get TriggerAuthentication -n p8` — can it reach the DB? `kubectl logs -n keda -l app=keda-operator` — errors? | Fix KEDA auth secret `p8-keda-pg-auth`, verify DB connection string in TriggerAuthentication matches the actual Postgres service. |
| **4. Workers** | `p8 admin health` → workers section. Any claims ever? Recent? | If KEDA is healthy but no claims, check the worker deployment image/command: `kubectl describe deployment p8-worker-small -n p8`. Local dev: `python -m p8.workers.processor --tier small`. |

Common failure modes:
- **"NO WORKERS — will never be claimed"**: KEDA not installed, ScaledObject not applied, or TriggerAuthentication can't connect to Postgres.
- **"OVERDUE by Xh"**: Workers can't keep up, or KEDA cooldown is too aggressive (currently 60s).
- **"STUCK processing for Xm"**: Worker crashed mid-task. `recover_stale_tasks` (every 5min) should reset these after 15min.
- **"last failed: Quota exceeded"**: User hit plan limits. Check `p8 admin quota --user <UUID>`.
- **"never scheduled"**: For dreaming, user has no recent messages/file uploads. For news, user has no `interests` or `categories` in metadata.
- **Reminders failing with "schema net does not exist"**: pg_net extension not installed. Required for reminder push notifications via HTTP from Postgres.
- **Reminders failing with "connection failed"**: pg_cron jobs have a hardcoded URL that pg_net can't reach. Jobs should use `current_setting('p8.internal_api_url')` GUC instead. Fix: restart the API (triggers self-heal) or set the GUC: `ALTER DATABASE p8db SET p8.internal_api_url = 'http://p8-api.p8.svc:8000';`

## KEDA Scaling

Each ScaledObject polls every 15s with 60s cooldown:
```sql
SELECT COALESCE(COUNT(*), 0) FROM task_queue
WHERE status = 'pending' AND tier = '$TIER' AND scheduled_at <= CURRENT_TIMESTAMP
```

## End-to-End Timeline

### File Upload and Processing

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant S as S3
    participant DB as PostgreSQL
    participant KD as KEDA
    participant W as Worker

    rect rgb(240, 248, 255)
    Note right of C: Upload and Enqueue
    C->>A: POST /files/upload
    A->>S: PUT object
    S-->>A: OK
    A->>DB: INSERT files row status=queued
    A->>DB: enqueue_file_task
    Note right of DB: task_queue: status=pending tier=small
    A-->>C: file_id
    end

    rect rgb(245, 245, 220)
    Note right of KD: KEDA Scaling
    loop poll every 15s
        KD->>DB: COUNT pending tasks for tier
    end
    DB-->>KD: 1 pending
    KD->>W: scale deployment 0 to 1
    end

    rect rgb(240, 255, 240)
    Note right of W: Claim and Process
    W->>DB: claim_tasks FOR UPDATE SKIP LOCKED
    Note right of DB: task_queue: status=processing
    W->>DB: check quota
    W->>S: download file
    S-->>W: file bytes
    W->>W: extract text and chunk
    W->>DB: INSERT resource chunks
    W->>DB: UPDATE files status=completed
    W->>DB: complete_task
    Note right of DB: task_queue: status=completed
    W->>DB: track usage
    end

    rect rgb(255, 245, 238)
    Note right of KD: Scale Down
    loop poll every 15s
        KD->>DB: COUNT pending = 0
    end
    Note right of KD: cooldown 60s
    KD->>W: scale deployment 1 to 0
    end
```

### Retry and Failure

```mermaid
sequenceDiagram
    participant W as Worker
    participant DB as PostgreSQL

    W->>DB: fail_task with error
    alt retries remaining
        Note right of DB: status=pending scheduled_at future
        Note right of DB: backoff 30s then 2m then 8m
        W->>DB: claim_tasks picks up retry
        W->>DB: complete_task or fail_task
    else max retries reached
        Note right of DB: status=failed permanent
    end
```

### pg_cron Housekeeping

```mermaid
sequenceDiagram
    participant CR as pg_cron
    participant DB as PostgreSQL

    Note right of CR: runs every 5 minutes
    CR->>DB: recover_stale_tasks
    Note right of DB: reset stuck processing tasks to pending

    Note right of CR: runs every hour
    CR->>DB: enqueue_dreaming_tasks
    Note right of DB: enqueue dreaming for active users
```

### Task State Machine

```mermaid
stateDiagram-v2
    [*] --> pending: enqueue
    pending --> processing: claim_tasks
    processing --> completed: complete_task
    processing --> pending: fail_task + retries left
    processing --> failed: fail_task + max retries
    processing --> pending: recover_stale_tasks
    completed --> [*]
    failed --> pending: admin reset
```
