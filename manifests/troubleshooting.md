# Troubleshooting

## pg_cron jobs failing with "connection failed"

**Symptom:** All pg_cron jobs show `status=failed, return_message="connection failed"` in `cron.job_run_details`. Zero successful runs.

**Root cause:** pg_cron connects to PostgreSQL via TCP (`cron.host = localhost`), not Unix sockets. If `pg_hba` only has a `local` (Unix socket) trust rule, the TCP connection falls through to the default CloudNativePG rule `host all all all scram-sha-256`. pg_cron does not supply passwords for job connections, so authentication fails silently.

**Diagnosis:**

```sql
-- Check job run history
SELECT jobid, status, return_message, start_time
FROM cron.job_run_details
ORDER BY start_time DESC LIMIT 20;

-- Count successes vs failures
SELECT status, COUNT(*) FROM cron.job_run_details GROUP BY status;
```

**Fix:** Add localhost TCP trust rules to the CloudNativePG cluster spec:

```yaml
postgresql:
  pg_hba:
    - local all all trust
    - host all all 127.0.0.1/32 trust
    - host all all ::1/128 trust
```

The CNPG operator will reload pg_hba after applying. Existing pg_cron jobs will start succeeding on their next scheduled run -- no restart needed for the cron extension itself.

**Why this is easy to miss:** pg_cron jobs that call `net.http_post()` fail at the database connection step, before the HTTP call is even attempted. The `cron.job_run_details` table shows the failure, but nothing else alerts on it. There are no application-level logs because the API is never reached.
