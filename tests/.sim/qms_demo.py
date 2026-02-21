#!/usr/bin/env python3
"""
qms_demo.py — Interactive QMS scheduling demo.

Connects directly to PostgreSQL (via port-forward or docker-compose),
enqueues tasks, and watches state transitions in real-time.

Usage:
    # Ensure port-forward or docker-compose is running
    kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw 5488:5432 &

    # Run demo
    python tests/.sim/qms_demo.py

    # Or with custom DB URL
    P8_DATABASE_URL=postgresql://user:pass@localhost:5488/p8db python tests/.sim/qms_demo.py

Environment:
    Reads from .env in project root (P8_DATABASE_URL).
    Assumes workers are running on the cluster to process tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── load .env from project root ──
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

try:
    import asyncpg
except ImportError:
    print("pip install asyncpg  (required for this demo)")
    sys.exit(1)

_raw_url = os.environ.get("P8_DATABASE_URL", "postgresql://p8:p8_dev@localhost:5488/p8")
# Disable SSL for port-forwarded connections (kubectl tunnel is already encrypted)
DATABASE_URL = _raw_url + ("&" if "?" in _raw_url else "?") + "sslmode=disable"

# ── terminal colors ──
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

STATUS_COLOR = {
    "pending": YELLOW,
    "processing": CYAN,
    "completed": GREEN,
    "failed": RED,
}


def banner(text: str) -> None:
    print(f"\n{BOLD}{MAGENTA}{'═' * 60}{RESET}")
    print(f"{BOLD}{MAGENTA}  {text}{RESET}")
    print(f"{BOLD}{MAGENTA}{'═' * 60}{RESET}")


def section(text: str) -> None:
    print(f"\n{BOLD}{CYAN}── {text} {'─' * max(1, 54 - len(text))}{RESET}")


def explain(text: str) -> None:
    for line in text.strip().splitlines():
        print(f"  {DIM}{line}{RESET}")


def status_str(s: str) -> str:
    c = STATUS_COLOR.get(s, RESET)
    return f"{c}{s}{RESET}"


async def show_queue_state(db: asyncpg.Connection, label: str = "Current queue state") -> None:
    section(label)
    rows = await db.fetch("""
        SELECT tier, status, COUNT(*) AS count
        FROM task_queue
        GROUP BY tier, status
        ORDER BY tier, CASE status
            WHEN 'pending' THEN 1 WHEN 'processing' THEN 2
            WHEN 'completed' THEN 3 WHEN 'failed' THEN 4 END
    """)
    if not rows:
        print(f"  {DIM}(empty — no tasks){RESET}")
        return
    print(f"  {BOLD}{'TIER':<10} {'STATUS':<14} {'COUNT':>5}{RESET}")
    for r in rows:
        print(f"  {r['tier']:<10} {status_str(r['status']):<24} {r['count']:>5}")


async def show_task(db: asyncpg.Connection, task_id: uuid.UUID) -> dict:
    row = await db.fetchrow("""
        SELECT id, task_type, tier, status, priority,
               retry_count, max_retries, error,
               result::text AS result,
               claimed_by,
               created_at, claimed_at, completed_at,
               scheduled_at
        FROM task_queue WHERE id = $1
    """, task_id)
    if not row:
        print(f"  {RED}Task {task_id} not found{RESET}")
        return {}
    r = dict(row)
    print(f"  {BOLD}id:{RESET}        {str(r['id'])[:8]}...")
    print(f"  {BOLD}type:{RESET}      {r['task_type']}")
    print(f"  {BOLD}tier:{RESET}      {r['tier']}")
    print(f"  {BOLD}status:{RESET}    {status_str(r['status'])}")
    print(f"  {BOLD}priority:{RESET}  {r['priority']}")
    print(f"  {BOLD}retries:{RESET}   {r['retry_count']}/{r['max_retries']}")
    if r["claimed_by"]:
        print(f"  {BOLD}worker:{RESET}   {r['claimed_by']}")
    if r["result"]:
        print(f"  {BOLD}result:{RESET}   {r['result'][:80]}")
    if r["error"]:
        print(f"  {BOLD}error:{RESET}    {RED}{r['error'][:80]}{RESET}")
    return r


async def poll_task(db: asyncpg.Connection, task_id: uuid.UUID,
                    target_status: str = "completed", timeout: float = 60) -> dict:
    """Poll a task until it reaches target_status or timeout."""
    start = asyncio.get_event_loop().time()
    prev_status = None
    while (asyncio.get_event_loop().time() - start) < timeout:
        row = await db.fetchrow(
            "SELECT status, claimed_by, result::text AS result, error FROM task_queue WHERE id = $1",
            task_id,
        )
        if row and row["status"] != prev_status:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  {DIM}[{ts}]{RESET} {status_str(row['status'])}", end="")
            if row["claimed_by"] and row["status"] == "processing":
                print(f"  {DIM}(worker: {row['claimed_by']}){RESET}", end="")
            if row["result"] and row["status"] == "completed":
                print(f"  {DIM}→ {row['result'][:60]}{RESET}", end="")
            if row["error"] and row["status"] in ("failed", "pending"):
                print(f"  {DIM}→ {row['error'][:40]}{RESET}", end="")
            print()
            prev_status = row["status"]
            if row["status"] == target_status:
                return dict(row)
            if row["status"] == "failed":
                return dict(row)
        await asyncio.sleep(1)
    print(f"  {RED}Timeout after {timeout}s{RESET}")
    return {}


def pause(msg: str = "Press Enter to continue") -> None:
    print(f"\n  {DIM}▸ {msg}...{RESET}", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print()


# ═══════════════════════════════════════════════════════════════
# DEMO SCENARIOS
# ═══════════════════════════════════════════════════════════════

async def run_demo():
    banner("QMS — Queue Management System Demo")
    explain(f"Connecting to: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else DATABASE_URL}")

    db = await asyncpg.connect(DATABASE_URL)

    # ── Architecture Overview ──
    banner("Architecture Overview")
    print(f"""
  {BOLD}PostgreSQL Tables{RESET}
  {DIM}─────────────────{RESET}
  {CYAN}task_queue{RESET}     The single source of truth for all background work.
                  Columns: id, task_type, tier, status, payload, priority,
                  scheduled_at, claimed_at, claimed_by, retry_count, result, error

  {BOLD}State Machine{RESET}
  {DIM}─────────────{RESET}
  {YELLOW}pending{RESET} ──▸ {CYAN}processing{RESET} ──▸ {GREEN}completed{RESET}
     ▴                  │
     └──── retry ◂──────┘  (if retry_count < max_retries, exponential backoff)
                        │
                        ▾
                    {RED}failed{RESET}      (max retries exceeded)

  {BOLD}Tiers & KEDA Scaling{RESET}
  {DIM}────────────────────{RESET}
  micro   │ 1 replica (always-on)   │ scheduled tasks, lightweight ops
  small   │ 0→5 (KEDA auto-scales)  │ files <1MB, dreaming
  medium  │ 0→3 (KEDA auto-scales)  │ files 1–50MB
  large   │ 0→2 (KEDA auto-scales)  │ files >=50MB

  {BOLD}KEDA Polling Query{RESET} (every 15s, per tier):
  {DIM}SELECT COUNT(*) FROM task_queue
  WHERE status = 'pending' AND tier = '$TIER' AND scheduled_at <= NOW(){RESET}

  When count > 0 → KEDA scales deployment from 0 → minReplicaCount+1
  When count = 0 for 60s → KEDA scales back to 0

  {BOLD}Worker Claim Pattern{RESET} (FOR UPDATE SKIP LOCKED):
  {DIM}UPDATE task_queue SET status='processing', claimed_by='$WORKER'
  WHERE id IN (
    SELECT id FROM task_queue
    WHERE status='pending' AND tier='$TIER' AND scheduled_at <= NOW()
    ORDER BY priority DESC, scheduled_at ASC
    LIMIT $BATCH FOR UPDATE SKIP LOCKED
  ){RESET}
""")
    pause()

    # ── Show Current State ──
    await show_queue_state(db, "Current queue state (before demo)")
    pause()

    # ═════════════════════════════════════════════════════
    # Scenario 1: Scheduled Task (micro tier)
    # ═════════════════════════════════════════════════════
    banner("Scenario 1: Scheduled Task → micro worker")
    explain("""
A scheduled task (kv_rebuild_incremental) is enqueued to the micro tier.
The micro worker is always-on (1 replica), so it claims this immediately.

Tables updated:
  1. INSERT into task_queue (status=pending)
  2. Worker claims: UPDATE status→processing, set claimed_at, claimed_by
  3. Handler runs kv_rebuild_incremental → calls rebuild_kv_store_incremental()
  4. On success: UPDATE status→completed, set result JSONB, completed_at
  5. The kv_store table is updated by the rebuild function
""")
    pause("Press Enter to enqueue scheduled task")

    task_id = await db.fetchval("""
        INSERT INTO task_queue (task_type, tier, payload)
        VALUES ('scheduled', 'micro', '{"action": "kv_rebuild_incremental"}'::jsonb)
        RETURNING id
    """)
    section(f"Enqueued task {str(task_id)[:8]}... — watching state transitions")
    result = await poll_task(db, task_id, timeout=30)

    section("Final task state")
    await show_task(db, task_id)
    pause()

    # ═════════════════════════════════════════════════════
    # Scenario 2: Batch of small tasks → KEDA scale-up
    # ═════════════════════════════════════════════════════
    banner("Scenario 2: Batch enqueue → KEDA scales small workers")
    explain("""
We enqueue 5 tasks to the 'small' tier. KEDA polls every 15s:

  SELECT COUNT(*) FROM task_queue
  WHERE status='pending' AND tier='small' AND scheduled_at <= NOW()

When count > 0, KEDA scales p8-worker-small from 0 → 1+ replicas.
Workers start, claim tasks via FOR UPDATE SKIP LOCKED (no contention),
process them, and after 60s cooldown with 0 pending, KEDA scales back to 0.

Run qms_monitor.sh in another terminal to watch KEDA and pods live.
""")
    pause("Press Enter to enqueue 5 small tasks")

    task_ids = []
    for i in range(5):
        tid = await db.fetchval("""
            INSERT INTO task_queue (task_type, tier, payload, priority)
            VALUES ('scheduled', 'small', $1::jsonb, $2)
            RETURNING id
        """, json.dumps({"action": "kv_rebuild_incremental", "batch_index": i}), 10 - i)
        task_ids.append(tid)
        print(f"  Enqueued {str(tid)[:8]}... priority={10 - i}")

    await show_queue_state(db, "After enqueueing 5 small tasks")

    section("Watching state transitions (KEDA will scale workers 0→N)")
    explain("""
KEDA polls every 15s — workers may take 15-30s to appear.
Watch the monitor terminal for pod creation events.
""")

    # Poll all tasks
    remaining = set(task_ids)
    start = asyncio.get_event_loop().time()
    timeout = 120
    while remaining and (asyncio.get_event_loop().time() - start) < timeout:
        for tid in list(remaining):
            row = await db.fetchrow(
                "SELECT status FROM task_queue WHERE id = $1", tid
            )
            if row and row["status"] in ("completed", "failed"):
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"  {DIM}[{ts}]{RESET} {str(tid)[:8]}... → {status_str(row['status'])}")
                remaining.discard(tid)
        if remaining:
            await asyncio.sleep(2)

    if remaining:
        print(f"  {YELLOW}{len(remaining)} tasks still pending after {timeout}s{RESET}")
        explain("This is expected if no workers are running (local-only mode).")
    else:
        print(f"  {GREEN}All 5 tasks completed!{RESET}")

    await show_queue_state(db, "After batch processing")
    pause()

    # ═════════════════════════════════════════════════════
    # Scenario 3: Task with retry (simulated failure)
    # ═════════════════════════════════════════════════════
    banner("Scenario 3: Retry with exponential backoff")
    explain("""
When a task fails, the fail_task() SQL function handles retry logic:

  retry_count < max_retries → reschedule with backoff:
    backoff = 30s × 4^retry_count
    retry 0: 30s,  retry 1: 2min,  retry 2: 8min,  retry 3: 32min

  retry_count >= max_retries → status = 'failed' (permanent)

State transitions for a task with max_retries=2:
  pending → processing → pending (retry 1, scheduled_at + 30s)
                       → pending (retry 2, scheduled_at + 2min)
                       → failed  (max retries exceeded)

Let's call fail_task() directly to demonstrate:
""")
    pause("Press Enter to create a task and simulate failures")

    # Create a task
    fail_id = await db.fetchval("""
        INSERT INTO task_queue (task_type, tier, payload, max_retries)
        VALUES ('scheduled', 'micro', '{"action": "test_retry"}'::jsonb, 2)
        RETURNING id
    """)
    print(f"  Created task {str(fail_id)[:8]}... (max_retries=2)")

    # Simulate claim + failure #1
    await db.execute("""
        UPDATE task_queue SET status = 'processing', claimed_at = NOW(),
        claimed_by = 'demo-worker' WHERE id = $1
    """, fail_id)
    print(f"  Claimed by demo-worker → {status_str('processing')}")

    await db.execute("SELECT fail_task($1, $2)", fail_id, "simulated error: connection timeout")
    row = await db.fetchrow(
        "SELECT status, retry_count, scheduled_at, error FROM task_queue WHERE id = $1", fail_id
    )
    delay = (row["scheduled_at"] - datetime.now(timezone.utc)).total_seconds()
    print(f"  fail_task() #1 → {status_str(row['status'])} "
          f"(retry {row['retry_count']}/2, backoff {delay:.0f}s)")
    print(f"  {DIM}error: {row['error']}{RESET}")

    # Failure #2
    await db.execute("UPDATE task_queue SET status='processing', claimed_at=NOW() WHERE id=$1", fail_id)
    await db.execute("SELECT fail_task($1, $2)", fail_id, "simulated error: still broken")
    row = await db.fetchrow(
        "SELECT status, retry_count, scheduled_at FROM task_queue WHERE id = $1", fail_id
    )
    if row["status"] == "pending":
        delay = (row["scheduled_at"] - datetime.now(timezone.utc)).total_seconds()
        print(f"  fail_task() #2 → {status_str(row['status'])} "
              f"(retry {row['retry_count']}/2, backoff {delay:.0f}s)")
    else:
        print(f"  fail_task() #2 → {status_str(row['status'])} "
              f"(retry {row['retry_count']}/2 — {RED}permanently failed{RESET})")

    # Failure #3 — should now be permanent
    if row["status"] == "pending":
        await db.execute("UPDATE task_queue SET status='processing', claimed_at=NOW() WHERE id=$1", fail_id)
        await db.execute("SELECT fail_task($1, $2)", fail_id, "simulated error: giving up")
        row = await db.fetchrow(
            "SELECT status, retry_count FROM task_queue WHERE id = $1", fail_id
        )
        print(f"  fail_task() #3 → {status_str(row['status'])} "
              f"(retry {row['retry_count']}/2 — {RED}permanently failed{RESET})")

    # Clean up the test task
    await db.execute("DELETE FROM task_queue WHERE id = $1", fail_id)
    print(f"  {DIM}(cleaned up test task){RESET}")
    pause()

    # ═════════════════════════════════════════════════════
    # Scenario 4: File processing with auto-tier
    # ═════════════════════════════════════════════════════
    banner("Scenario 4: File processing — auto-tier by size")
    explain("""
enqueue_file_task(file_id) is a SQL function that:
  1. Looks up the file in the 'files' table (size_bytes, uri, name, mime_type)
  2. Auto-assigns tier:  <1MB → small,  1-50MB → medium,  >=50MB → large
  3. Inserts into task_queue with payload = {file_id, uri, name, mime_type, size_bytes}
  4. Small files get priority=10 (processed first)

The FileProcessingHandler then:
  1. Downloads file content from S3 (ctx.file_service.read(uri))
  2. Calls ctx.content_service.ingest() → extract text → chunk → persist
  3. Returns {bytes_processed, chunks, total_chars, file_id}

We can simulate by inserting directly (skipping the files table lookup):
""")
    pause("Press Enter to enqueue simulated file tasks")

    for label, size, tier in [("small_doc.pdf", 500_000, "small"),
                               ("report.pdf", 5_000_000, "medium"),
                               ("dataset.csv", 100_000_000, "large")]:
        payload = json.dumps({
            "file_id": str(uuid.uuid4()),
            "name": label,
            "size_bytes": size,
            "mime_type": "application/pdf",
        })
        priority = 10 if size < 1_048_576 else 0
        tid = await db.fetchval("""
            INSERT INTO task_queue (task_type, tier, payload, priority)
            VALUES ('file_processing', $1, $2::jsonb, $3)
            RETURNING id
        """, tier, payload, priority)
        print(f"  {str(tid)[:8]}... → tier={BOLD}{tier}{RESET}  {label} ({size:,} bytes)")

    await show_queue_state(db, "After file task enqueue")
    explain("""
KEDA will now detect pending tasks in small/medium/large tiers
and scale up the corresponding worker deployments.

In production, the FileProcessingHandler would download from S3
and process each file. Here we'll clean up the simulated tasks.
""")
    pause("Press Enter to clean up simulated file tasks")
    await db.execute("DELETE FROM task_queue WHERE task_type='file_processing' AND payload->>'name' IN ('small_doc.pdf','report.pdf','dataset.csv')")
    print(f"  {DIM}(cleaned up simulated file tasks){RESET}")
    pause()

    # ═════════════════════════════════════════════════════
    # Scenario 5: pg_cron scheduled enqueue
    # ═════════════════════════════════════════════════════
    banner("Scenario 5: pg_cron — automated scheduling")
    explain("""
Two pg_cron jobs run inside PostgreSQL (no K8s CronJobs needed):

  ┌─────────────────────┬────────────┬────────────────────────────────────┐
  │ Job                 │ Schedule   │ What it does                       │
  ├─────────────────────┼────────────┼────────────────────────────────────┤
  │ qms-recover-stale   │ */5 * * * *│ recover_stale_tasks(15)            │
  │                     │            │ Reset tasks stuck in 'processing'  │
  │                     │            │ for >15 min back to 'pending'      │
  ├─────────────────────┼────────────┼────────────────────────────────────┤
  │ qms-dreaming-enqueue│ 0 * * * *  │ enqueue_dreaming_tasks()           │
  │                     │            │ One dreaming task per active user   │
  │                     │            │ (skips if pending dreaming exists) │
  └─────────────────────┴────────────┴────────────────────────────────────┘

  These run as PostgreSQL background jobs via pg_cron extension.
""")

    # Show pg_cron jobs
    section("pg_cron jobs")
    try:
        jobs = await db.fetch("SELECT jobid, jobname, schedule, command FROM cron.job ORDER BY jobid")
        if jobs:
            print(f"  {BOLD}{'ID':<4} {'NAME':<25} {'SCHEDULE':<14} {'COMMAND'}{RESET}")
            for j in jobs:
                print(f"  {j['jobid']:<4} {j['jobname']:<25} {j['schedule']:<14} {j['command'][:50]}")
        else:
            print(f"  {YELLOW}No pg_cron jobs found — run sql/03_qms.sql to install{RESET}")
    except asyncpg.InsufficientPrivilegeError:
        print(f"  {DIM}(cron.job requires superuser — connect as postgres to view){RESET}")
        explain("""
Jobs are installed via sql/03_qms.sql:
  cron.schedule('qms-recover-stale',   '*/5 * * * *', 'SELECT recover_stale_tasks(15)')
  cron.schedule('qms-dreaming-enqueue', '0 * * * *',  'SELECT enqueue_dreaming_tasks()')
""")
    pause()

    # ═════════════════════════════════════════════════════
    # Summary
    # ═════════════════════════════════════════════════════
    banner("Summary — What happens end-to-end")
    print(f"""
  {BOLD}1. Task Enqueue{RESET}
     INSERT into {CYAN}task_queue{RESET} with status={YELLOW}pending{RESET}
     Source: API endpoint, pg_cron job, or direct SQL

  {BOLD}2. KEDA Detection{RESET} (every 15s)
     Polls: SELECT COUNT(*) FROM task_queue WHERE status='pending' AND tier='$TIER'
     Scales worker deployment from 0 → N replicas

  {BOLD}3. Worker Startup{RESET}
     Pod starts → initContainer waits for PostgreSQL → worker connects
     Registers handlers: file_processing, dreaming, scheduled

  {BOLD}4. Task Claim{RESET} (FOR UPDATE SKIP LOCKED)
     Worker calls claim_tasks(tier, worker_id, batch_size)
     UPDATE {CYAN}task_queue{RESET}: status={CYAN}processing{RESET}, claimed_by=worker-xxxx

  {BOLD}5. Handler Execution{RESET}
     Dispatches to handler by task_type
     Handler does the actual work (file extraction, dreaming, KV rebuild)

  {BOLD}6. Completion or Retry{RESET}
     Success: UPDATE {CYAN}task_queue{RESET}: status={GREEN}completed{RESET}, result=JSONB
     Failure: fail_task() → retry with backoff or status={RED}failed{RESET}

  {BOLD}7. KEDA Cooldown{RESET} (60s with 0 pending)
     Scales worker deployment back to 0 replicas

  {BOLD}8. pg_cron Housekeeping{RESET}
     Every 5 min: recover_stale_tasks() resets stuck tasks
     Every hour: enqueue_dreaming_tasks() for active users
""")

    await show_queue_state(db, "Final queue state")
    await db.close()
    print(f"\n{BOLD}{GREEN}Demo complete.{RESET}\n")


if __name__ == "__main__":
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        print(f"\n{DIM}Interrupted.{RESET}")
    except asyncpg.PostgresError as e:
        print(f"\n{RED}Database error: {e}{RESET}")
        print(f"{DIM}Ensure PostgreSQL is accessible and port-forward is running.{RESET}")
        sys.exit(1)
