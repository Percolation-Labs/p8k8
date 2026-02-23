# p8 admin CLI

Admin commands for pipeline diagnostics, queue inspection, quota management, and task enqueuing.

**Defaults to remote** (Hetzner p8-w-1 via port-forward on `localhost:5490`). Use `--local` for the local docker-compose DB. See `docs/services.md` for full architecture and troubleshooting.

## Setup (remote)

```bash
# Open port-forwards (standard ports: 5490 for Postgres, 8201 for Vault)
kubectl --context=p8-w-1 -n p8 port-forward svc/p8-postgres-rw 5490:5432 &
kubectl --context=p8-w-1 -n p8 port-forward svc/openbao 8201:8200 &

# If the remote DB requires auth, set P8_DATABASE_URL:
PG_PASS=$(kubectl --context=p8-w-1 -n p8 get secret p8-database-credentials \
  -o jsonpath='{.data.password}' | base64 -d)
export P8_DATABASE_URL="postgresql://p8user:${PG_PASS}@localhost:5490/p8db?sslmode=disable"
```

If the port-forward isn't open, commands exit with instructions.

## Commands

```bash
# Health — the primary diagnostic (pipeline + per-user task status)
p8 admin health                        # All users
p8 admin health --email amartey        # Single user by email (partial match)
p8 admin health --user <UUID>          # Single user by id or user_id

# Queue — raw task inspection
p8 admin queue                         # Pending tasks aggregated by tenant
p8 admin queue --status failed         # Failed tasks
p8 admin queue --detail                # Individual tasks with IDs, retries, age
p8 admin queue --detail -t dreaming    # Filter by task type

# Quota — usage report and reset
p8 admin quota                         # All users with progress bars
p8 admin quota --user <UUID>           # Single user
p8 admin quota --reset --user <UUID>   # Reset all current-period counters
p8 admin quota --reset --user <UUID> --resource chat_tokens

# Enqueue — manually trigger a task for a user
p8 admin enqueue reading_summary --user <UUID>             # Run immediately
p8 admin enqueue dreaming --user <UUID> --delay 5          # Run in 5 minutes
p8 admin enqueue news --user <UUID>                        # News digest

# Local dev
p8 admin --local health
p8 admin --local queue --status failed
```

## Health check details

The `health` command checks a 5-stage pipeline:

| Stage | What it checks |
|-------|---------------|
| **pg_net GUC** | `p8.internal_api_url` is set — required for all pg_cron HTTP jobs |
| **pg_net jobs** | Reminder jobs use GUC-based URLs (not hardcoded domains) |
| **pg_cron** | `qms-dreaming-enqueue` and `qms-news-enqueue` exist and are active |
| **task_queue** | Pending tasks due now, by tier |
| **workers** | Active workers (claimed tasks recently) |

If `pg_net GUC` shows MISSING, fix with:
```sql
ALTER DATABASE p8db SET p8.internal_api_url = 'http://p8-api.p8.svc:8000';
```

If `pg_net jobs` shows STALE, restart the API (self-heal runs on boot) or re-deploy.

## Teardown

```bash
pkill -f "port-forward svc/p8-postgres-rw"
pkill -f "port-forward svc/openbao"
```
